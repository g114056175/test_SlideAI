import difflib
import json
import logging
import math
import os
import re
import subprocess
import sys
import tempfile
import textwrap
import threading
import signal
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from backend.app.services.alignment.subtitle_builder import build_srt
from backend.app.services.alignment.time_mapper import (
    align_reference_chars,
    estimate_char_times_from_words,
    expand_units_to_char_spans,
)
from backend.app.services.alignment.repair import (
    make_repair_plan,
    map_repaired_spans_to_source,
)
from backend.app.services.alignment.sentence_splitter import (
    _ASCII_WORD_RE,
    _CJK_RE,
    _JP_RE,
    _align_clean,
    _clean_display_text,
    _has_cjk,
    _is_cjk_char,
    _join_tokens,
    _split_for_readability,
)

logger = logging.getLogger("video_abstract")

_ALIGN_WORKER: Optional[subprocess.Popen] = None
_ALIGN_WORKER_LOCK = threading.RLock()
_ALIGN_IDLE_TIMER: Optional[threading.Timer] = None
_ALIGN_WORKER_PREFIX = "__QWEN_ALIGN_RESULT__"
_ALIGN_IDLE_TIMEOUT_SEC = max(0, int(os.getenv("QWEN3_ALIGNMENT_IDLE_TIMEOUT_SEC", "60")))


def _stop_alignment_worker() -> None:
    """Terminate the worker process group so its model VRAM is actually freed."""
    global _ALIGN_WORKER, _ALIGN_IDLE_TIMER
    with _ALIGN_WORKER_LOCK:
        if _ALIGN_IDLE_TIMER:
            _ALIGN_IDLE_TIMER.cancel()
            _ALIGN_IDLE_TIMER = None
        if _ALIGN_WORKER and _ALIGN_WORKER.poll() is None:
            try:
                os.killpg(_ALIGN_WORKER.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        _ALIGN_WORKER = None


def release_alignment_worker() -> None:
    """Immediately release alignment/ASR VRAM before another speech stage."""
    _stop_alignment_worker()


def _schedule_alignment_idle_shutdown() -> None:
    global _ALIGN_IDLE_TIMER
    if _ALIGN_IDLE_TIMEOUT_SEC <= 0:
        return
    with _ALIGN_WORKER_LOCK:
        if _ALIGN_IDLE_TIMER:
            _ALIGN_IDLE_TIMER.cancel()
        _ALIGN_IDLE_TIMER = threading.Timer(_ALIGN_IDLE_TIMEOUT_SEC, _stop_alignment_worker)
        _ALIGN_IDLE_TIMER.daemon = True
        _ALIGN_IDLE_TIMER.start()


def _ensure_alignment_worker(runtime_python: str, hf_token: Optional[str]) -> subprocess.Popen:
    global _ALIGN_WORKER, _ALIGN_IDLE_TIMER
    if _ALIGN_IDLE_TIMER:
        _ALIGN_IDLE_TIMER.cancel()
        _ALIGN_IDLE_TIMER = None
    if _ALIGN_WORKER and _ALIGN_WORKER.poll() is None:
        return _ALIGN_WORKER
    worker_path = Path(__file__).with_name("qwen_alignment_worker.py")
    if not worker_path.is_file():
        raise RuntimeError(f"alignment worker not found: {worker_path}")
    env = os.environ.copy()
    if hf_token:
        env["HF_TOKEN"] = hf_token
        env["HUGGINGFACE_HUB_TOKEN"] = hf_token
    _ALIGN_WORKER = subprocess.Popen(
        [runtime_python, str(worker_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        bufsize=1,
        env=env,
        start_new_session=True,
    )
    ready = _ALIGN_WORKER.stdout.readline().strip() if _ALIGN_WORKER.stdout else ""
    if not ready.startswith(_ALIGN_WORKER_PREFIX):
        _stop_alignment_worker()
        raise RuntimeError("Qwen alignment worker failed to become ready")
    return _ALIGN_WORKER


def _run_word_timestamp_worker(
    *,
    runtime_python: str,
    audio_path: str,
    language: str,
    alignment_mode: str,
    text: str,
    hf_token: Optional[str] = None,
) -> Tuple[List[Dict[str, Any]], str, str]:
    """Send one request to the lazy persistent alignment worker."""
    # TTS and ASR do not need to coexist in this sequential pipeline. Releasing
    # Nano first prevents a transient double-model VRAM peak.
    for module_name, function_name, label in (
        ("backend.app.services.voxtts", "release_voxtts_worker", "Nano TTS"),
        ("backend.app.services.qwen3_tts", "release_qwen3_tts_worker", "Qwen3 TTS"),
    ):
        try:
            module = __import__(module_name, fromlist=[function_name])
            getattr(module, function_name)()
        except Exception as exc:
            logger.warning("[SubtitleAlign] could not release %s worker: %s", label, exc)

    with _ALIGN_WORKER_LOCK:
        try:
            worker = _ensure_alignment_worker(runtime_python, hf_token)
            if not worker.stdin or not worker.stdout:
                raise RuntimeError("Qwen alignment worker pipes unavailable")
            request_id = uuid.uuid4().hex
            worker.stdin.write(json.dumps({
                "id": request_id,
                "audio_path": audio_path,
                "language": language,
                "alignment_mode": alignment_mode,
                "text": text,
            }, ensure_ascii=False) + "\n")
            worker.stdin.flush()
            while True:
                line = worker.stdout.readline()
                if not line:
                    _stop_alignment_worker()
                    raise RuntimeError("Qwen alignment worker exited during inference")
                if not line.startswith(_ALIGN_WORKER_PREFIX):
                    continue
                payload = json.loads(line[len(_ALIGN_WORKER_PREFIX):])
                if payload.get("id") == request_id:
                    break
            if not payload.get("ok"):
                raise RuntimeError(str(payload.get("error") or "qwen alignment worker failed")[:1200])
            words = payload.get("words") or []
            if not words:
                raise RuntimeError("alignment worker returned no words")
            return words, str(payload.get("backend") or "unknown"), str(payload.get("asr_text") or "")
        finally:
            _schedule_alignment_idle_shutdown()

@dataclass
class AlignmentResult:
    segments: List[Dict[str, Any]]
    srt: str
    backend: str
    audio_duration: float
    warning: str = ""
    readable_chunks: List[str] = None
    source_text: str = ""

_OPENCC_T2S = None
_OPENCC_S2T = None
_OPENCC_IMPORT_FAILED = False

def _normalize_alignment_mode(mode: str) -> str:
    val = str(mode or "").strip().lower()
    if val in {"", "auto", "smart"}:
        return "auto"
    if val in {"auto_asr", "asr", "auto"}:
        return "auto_asr"
    return "scripted"


def _normalize_language_name(language: str) -> str:
    val = str(language or "").strip().lower()
    if not val or val in {"auto", "automatic"}:
        return "auto"
    if val.startswith("zh") or "chinese" in val:
        return "Chinese"
    if val.startswith("en") or "english" in val:
        return "English"
    if val.startswith("ja") or "japanese" in val:
        return "Japanese"
    if val.startswith("ko") or "korean" in val:
        return "Korean"
    if val.startswith("fr") or "french" in val:
        return "French"
    if val.startswith("de") or "german" in val:
        return "German"
    if val.startswith("es") or "spanish" in val:
        return "Spanish"
    if val.startswith("it") or "italian" in val:
        return "Italian"
    if val.startswith("ru") or "russian" in val:
        return "Russian"
    if val.startswith("pt") or "portuguese" in val:
        return "Portuguese"
    return "auto"


def _infer_language_from_text(text: str) -> str:
    src = str(text or "")
    if _JP_RE.search(src):
        return "Japanese"
    if _CJK_RE.search(src):
        return "Chinese"
    return "English"


def _get_opencc_converters() -> Tuple[Optional[Any], Optional[Any]]:
    global _OPENCC_T2S, _OPENCC_S2T, _OPENCC_IMPORT_FAILED
    if _OPENCC_IMPORT_FAILED:
        return None, None
    if _OPENCC_T2S is not None and _OPENCC_S2T is not None:
        return _OPENCC_T2S, _OPENCC_S2T
    try:
        from opencc import OpenCC  # type: ignore
        _OPENCC_T2S = OpenCC("t2s")
        _OPENCC_S2T = OpenCC("s2t")
        return _OPENCC_T2S, _OPENCC_S2T
    except Exception:
        _OPENCC_IMPORT_FAILED = True
        return None, None


def _convert_char_by_char(text: str, converter: Any) -> str:
    """Convert text character-by-character using OpenCC to avoid phrase-level substitutions.
    This preserves user-specific terminology (e.g. 分布式 won't become 分散式).
    """
    if not text:
        return text
    result = []
    for ch in text:
        try:
            result.append(converter.convert(ch))
        except Exception:
            result.append(ch)
    return "".join(result)


def _to_simplified_for_qwen(text: str) -> str:
    """Convert Traditional Chinese to Simplified for Qwen input (character-by-character)."""
    if not text:
        return text
    t2s, _ = _get_opencc_converters()
    if t2s is None:
        return text
    try:
        return _convert_char_by_char(text, t2s)
    except Exception:
        return text


def _to_traditional_for_display(text: str) -> str:
    """Convert Simplified Chinese back to Traditional for display (character-by-character)."""
    if not text:
        return text
    _, s2t = _get_opencc_converters()
    if s2t is None:
        return text
    try:
        return _convert_char_by_char(text, s2t)
    except Exception:
        return text


def _normalize_split_limits(min_chars: int, max_chars: int) -> Tuple[int, int]:
    lo = max(1, int(min_chars or 1))
    hi = max(1, int(max_chars or 1))
    if lo > hi:
        lo, hi = hi, lo
    return lo, hi


def _tokenize_display_units(text: str) -> List[str]:
    """Tokenize text into display units for word-level highlighting.
    Groups punctuation and spaces with adjacent speech tokens so they 
    are preserved in the frontend display.
    """
    src = str(text or "")
    tokens: List[str] = []
    i = 0
    n = len(src)
    prefix = ""
    percent_re = re.compile(r"\d+(?:\.\d+)?%")
    version_re = re.compile(r"[vV]?\d+(?:\.\d+)+(?:-[A-Za-z0-9_.-]+)?")
    dotted_word_re = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*(?:\.[A-Za-z0-9_-]+)+")
    number_before_unit_re = re.compile(r"\d+(?:\.\d+)?(?=[A-Za-z]+)")
    decimal_re = re.compile(r"\d+\.\d+")
    while i < n:
        ch = src[i]
        if ch.isspace():
            if tokens:
                tokens[-1] += ch
            else:
                prefix += ch
            i += 1
            continue
        m = percent_re.match(src, i)
        if m:
            tokens.append(prefix + m.group(0))
            prefix = ""
            i = m.end()
            continue
        m = version_re.match(src, i)
        if m:
            tokens.append(prefix + m.group(0))
            prefix = ""
            i = m.end()
            continue
        m = dotted_word_re.match(src, i)
        if m:
            tokens.append(prefix + m.group(0))
            prefix = ""
            i = m.end()
            continue
        m = number_before_unit_re.match(src, i)
        if m:
            tokens.append(prefix + m.group(0))
            prefix = ""
            i = m.end()
            continue
        m = decimal_re.match(src, i)
        if m:
            tokens.append(prefix + m.group(0))
            prefix = ""
            i = m.end()
            continue
        m = _ASCII_WORD_RE.match(src, i)
        if m:
            tokens.append(prefix + m.group(0))
            prefix = ""
            i = m.end()
            continue
        if _is_cjk_char(ch):
            tokens.append(prefix + ch)
            prefix = ""
            i += 1
            continue
        # Punctuation/symbols
        if tokens:
            tokens[-1] += ch
        else:
            prefix += ch
        i += 1
        
    if prefix and tokens:
        tokens[-1] += prefix
    elif prefix and not tokens:
        # If it's ONLY punctuation (edge case)
        tokens.append(prefix)
        
    return tokens


def _build_display_words(
    seg_text: str,
    seg_spans: List[Tuple[float, float]],
) -> List[Dict[str, Any]]:
    """Build per-word timing entries for karaoke highlighting.
    seg_spans must have exactly len(_align_clean(seg_text)) entries,
    one per speech character (no punctuation).
    """
    words: List[Dict[str, Any]] = []
    # Use align_clean length for consistency: seg_spans is indexed by
    # punctuation-free character count (matches _align_clean cursor tracking).
    align_text = _align_clean(seg_text)
    if not align_text or not seg_spans or len(align_text) != len(seg_spans):
        return words

    cursor = 0
    for tok in _tokenize_display_units(seg_text):
        # _tokenize_display_units already skips punctuation; use _align_clean
        # to count only the chars that have aligner timestamps.
        tok_len = len(_align_clean(tok))
        if tok_len <= 0:
            continue
        s_idx = cursor
        e_idx = min(cursor + tok_len - 1, len(seg_spans) - 1)
        start = float(seg_spans[s_idx][0])
        end = float(seg_spans[e_idx][1])
        if end <= start:
            end = start + 1e-3
        words.append({
            "text": tok,
            "start": start,
            "end": end,
        })
        cursor += tok_len
        if cursor >= len(seg_spans):
            break
    return words


def _build_display_words_fallback(
    seg_text: str,
    seg_spans: List[Tuple[float, float]],
) -> List[Dict[str, Any]]:
    """
    Best-effort fallback when text length and span length do not match exactly.
    Proportionally maps display units onto available char spans to avoid empty words.
    Uses _align_clean (punctuation-free) lengths for consistent index arithmetic.
    """
    align_text = _align_clean(seg_text)
    if not align_text or not seg_spans:
        return []
    n = len(align_text)
    m = len(seg_spans)
    words: List[Dict[str, Any]] = []

    cursor = 0
    for tok in _tokenize_display_units(seg_text):
        tok_len = len(_align_clean(tok))
        if tok_len <= 0:
            continue
        s_pos = cursor
        e_pos = cursor + tok_len - 1
        s_idx = min(m - 1, max(0, int((s_pos * m) / max(n, 1))))
        e_idx = min(m - 1, max(s_idx, int(((e_pos + 1) * m - 1) / max(n, 1))))
        start = float(seg_spans[s_idx][0])
        end = float(seg_spans[e_idx][1])
        if end <= start:
            end = start + 1e-3
        words.append({"text": tok, "start": start, "end": end})
        cursor += tok_len
        if cursor >= n:
            break
    return words


def _build_srt(segments: List[Dict[str, Any]]) -> str:
    return build_srt(segments)


def _run_word_timestamp_backend(
    *,
    runtime_python: str,
    audio_path: str,
    language: str,
    alignment_mode: str,
    text: str,
    hf_token: Optional[str] = None,
) -> Tuple[List[Dict[str, Any]], str, str]:
    script = textwrap.dedent("""
import json
import os
import sys

audio_path = sys.argv[1]
language = (sys.argv[2] or "").lower().strip()
alignment_mode = (sys.argv[3] or "scripted").strip().lower()
input_text = sys.argv[4] or ""

def normalize_lang(lang):
    if lang.startswith("zh") or "chinese" in lang:
        return "Chinese"
    if lang.startswith("en") or "english" in lang:
        return "English"
    if lang.startswith("ja") or "japanese" in lang:
        return "Japanese"
    if lang.startswith("ko") or "korean" in lang:
        return "Korean"
    if lang.startswith("fr") or "french" in lang:
        return "French"
    if lang.startswith("de") or "german" in lang:
        return "German"
    if lang.startswith("es") or "spanish" in lang:
        return "Spanish"
    if lang.startswith("it") or "italian" in lang:
        return "Italian"
    if lang.startswith("ru") or "russian" in lang:
        return "Russian"
    if lang.startswith("pt") or "portuguese" in lang:
        return "Portuguese"
    return None

lang_name = normalize_lang(language) or "Chinese"
words = []
backend = "qwen3-forced-aligner"
asr_text = ""

try:
    import torch
    from qwen_asr import Qwen3ASRModel, Qwen3ForcedAligner
except Exception as e:
    print(f"qwen_asr_not_available: {e}", file=sys.stderr)
    sys.exit(2)

device_map = os.getenv("QWEN3_ASR_DEVICE", "cuda:0" if torch.cuda.is_available() else "cpu")
dtype_env = (os.getenv("QWEN3_ASR_DTYPE", "bfloat16" if torch.cuda.is_available() else "float32")).lower().strip()
dtype = getattr(torch, dtype_env, torch.bfloat16 if torch.cuda.is_available() else torch.float32)
attn_impl = os.getenv("QWEN3_ASR_ATTN_IMPL", "eager").strip()

asr_ckpt = os.getenv("QWEN3_ASR_MODEL_PATH", "").strip() or os.getenv("QWEN3_ASR_MODEL_ID", "Qwen/Qwen3-ASR-1.7B")
aligner_ckpt = os.getenv("QWEN3_ALIGNER_MODEL_PATH", "").strip() or os.getenv("QWEN3_ALIGNER_MODEL_ID", "Qwen/Qwen3-ForcedAligner-0.6B")

def _is_zh(lang):
    x = str(lang or "").lower()
    return "chinese" in x or x.startswith("zh")

def _has_cjk(text):
    if not text:
        return False
    for ch in str(text):
        if "\\u3400" <= ch <= "\\u9fff":
            return True
    return False

def _to_simplified(text):
    if not text:
        return text
    try:
        from opencc import OpenCC
        return OpenCC("t2s").convert(text)
    except Exception:
        return text

def _to_traditional(text):
    if not text:
        return text
    try:
        from opencc import OpenCC
        return OpenCC("s2t").convert(text)
    except Exception:
        return text

common_kwargs = {
    "device_map": device_map,
    "dtype": dtype,
}
if attn_impl:
    common_kwargs["attn_implementation"] = attn_impl

if alignment_mode == "auto_asr":
    backend = "qwen3-asr+forced-aligner"
    asr_model = Qwen3ASRModel.from_pretrained(
        asr_ckpt,
        forced_aligner=aligner_ckpt,
        forced_aligner_kwargs=dict(common_kwargs),
        **common_kwargs,
    )
    result = asr_model.transcribe(
        audio=audio_path,
        language=None,
        return_time_stamps=True,
    )[0]
    asr_text = str(getattr(result, "text", "") or "")
    ts = getattr(result, "time_stamps", None)
    items = list(ts) if ts is not None else []
else:
    backend = "qwen3-forced-aligner"
    if not input_text.strip():
        print("scripted mode requires non-empty text", file=sys.stderr)
        sys.exit(3)
    qwen_text = _to_simplified(input_text) if _is_zh(lang_name) else input_text
    aligner = Qwen3ForcedAligner.from_pretrained(
        aligner_ckpt,
        **common_kwargs,
    )
    result = aligner.align(
        audio=audio_path,
        text=qwen_text,
        language=lang_name,
    )[0]
    asr_text = str(qwen_text)
    items = list(result)

for it in items:
    start = float(getattr(it, "start_time", 0.0) or 0.0)
    end = float(getattr(it, "end_time", start) or start)
    txt = str(getattr(it, "text", "") or "").strip()
    if not txt:
        continue
    if end <= start:
        end = start + 1e-3
    words.append({
        "text": txt,
        "start": start,
        "end": end,
    })

need_traditional = _is_zh(lang_name) or _has_cjk(asr_text) or any(_has_cjk(w.get("text")) for w in words)
if need_traditional:
    asr_text = _to_traditional(asr_text)
    for w in words:
        w["text"] = _to_traditional(str(w.get("text") or ""))

if not words:
    print("qwen3_alignment_returned_no_words", file=sys.stderr)
    sys.exit(3)

print(json.dumps({"backend": backend, "words": words, "asr_text": asr_text}, ensure_ascii=False))
""")
    env = os.environ.copy()
    if hf_token:
        env["HF_TOKEN"] = hf_token
        env["HUGGINGFACE_HUB_TOKEN"] = hf_token

    proc = subprocess.run(
        [runtime_python, "-c", script, audio_path, language or "", alignment_mode or "", text or ""],
        capture_output=True,
        text=True,
        timeout=600,
        env=env,
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "qwen-asr alignment failed").strip()[:1200])
    raw_out = (proc.stdout or "").strip()
    payload = None
    # Some runtimes may print extra informational lines before JSON.
    for line in reversed(raw_out.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
            break
        except Exception:
            continue
    if payload is None:
        try:
            # Fallback: try parsing from the last JSON object marker.
            i = raw_out.rfind('{"backend"')
            if i >= 0:
                payload = json.loads(raw_out[i:])
        except Exception:
            payload = None
    if payload is None:
        raise RuntimeError("invalid alignment backend output: no json payload found")
    words = payload.get("words") or []
    backend = payload.get("backend") or "unknown"
    asr_text = str(payload.get("asr_text") or "")
    if not words:
        raise RuntimeError(f"alignment backend returned no words ({backend})")
    return words, backend, asr_text


def _estimate_char_times_from_words(words: List[Dict[str, Any]]) -> Tuple[str, List[Tuple[float, float]], float]:
    return estimate_char_times_from_words(words)


def _align_reference_chars(
    ref_text_clean: str,
    asr_text: str,
    asr_spans: List[Tuple[float, float]],
    audio_end: float,
) -> Tuple[List[Tuple[float, float]], float]:
    return align_reference_chars(ref_text_clean, asr_text, asr_spans, audio_end)


def _expand_units_to_char_spans(units: List[Dict[str, Any]]) -> Tuple[str, List[Tuple[float, float]]]:
    return expand_units_to_char_spans(units)


def align_subtitles_from_audio_and_text(
    *,
    text: str,
    audio_bytes: bytes,
    audio_filename: str = "input.wav",
    language: str = "auto",
    alignment_mode: str = "auto",
    split_min_chars: int = 6,
    split_max_chars: int = 24,
    enable_pause_split: bool = False,
    pause_threshold_ms: int = 320,
) -> AlignmentResult:
    mode = _normalize_alignment_mode(alignment_mode)
    min_chars, max_chars = _normalize_split_limits(split_min_chars, split_max_chars)

    ref_clean = _clean_display_text(text or "")
    if mode == "auto":
        mode = "scripted" if ref_clean else "auto_asr"
    if mode == "scripted" and not ref_clean:
        # Fallback to ASR flow when callers keep scripted but send empty text.
        mode = "auto_asr"
    if not audio_bytes:
        raise ValueError("audio file is empty")

    language_name = _normalize_language_name(language)
    if language_name == "auto" and mode == "scripted":
        language_name = _infer_language_from_text(text)
    elif language_name == "auto":
        language_name = "auto"

    qwen_input_text = str(text or "")

    default_backend_python = str(Path(__file__).resolve().parents[2] / ".venv" / "bin" / "python")
    runtime_python = (
        os.getenv("QWEN3_ASR_RUNTIME_PYTHON", "").strip()
        or os.getenv("QWEN3_TTS_RUNTIME_PYTHON", "").strip()
        or default_backend_python
    )
    if not os.path.isfile(runtime_python):
        raise RuntimeError(f"runtime python not found: {runtime_python}")

    suffix = os.path.splitext(audio_filename or "")[-1] or ".wav"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as fp:
        fp.write(audio_bytes)
        audio_path = fp.name

    try:
        hf_token = (os.getenv("HF_TOKEN") or os.getenv("HUGGINGFACE_HUB_TOKEN") or "").strip() or None
        words, backend, backend_text = _run_word_timestamp_worker(
            runtime_python=runtime_python,
            audio_path=audio_path,
            language=language_name,
            alignment_mode=mode,
            text=qwen_input_text,
            hf_token=hf_token,
        )

        # ── 強對齊失敗早期檢測（Phase 1：時間覆蓋率檢查） ──────────────────────
        # 取得音頻實際長度，若對齊結果的最大時間遠小於實際音頻長度，
        # 代表強對齊失敗（通常是文稿與音頻完全不符），直接回報錯誤。
        actual_audio_duration = 0.0
        try:
            import soundfile as _sf
            with _sf.SoundFile(audio_path) as _f:
                actual_audio_duration = len(_f) / max(_f.samplerate, 1)
        except Exception:
            try:
                import wave as _wave
                with _wave.open(audio_path, 'r') as _wf:
                    actual_audio_duration = _wf.getnframes() / max(_wf.getframerate(), 1)
            except Exception:
                pass

        if actual_audio_duration > 3.0 and words:
            max_word_end = max((float(w.get("end") or 0) for w in words), default=0.0)
            # 若對齊輸出的最大時間不到實際音頻長度的 15%，視為強對齊嚴重失敗
            if max_word_end < actual_audio_duration * 0.15:
                detail = (
                    f"⚠️ 強對齊失敗：文稿與音頻內容嚴重不符"
                    f"（對齊覆蓋 {max_word_end:.1f}s／音頻 {actual_audio_duration:.1f}s，"
                    f"覆蓋率 {max_word_end/actual_audio_duration*100:.0f}%）。\n"
                    f"請確認您提供的講稿是否與這段音頻一致，"
                    f"或將文稿欄位留空改用 ASR 自動辨識。"
                )
                from fastapi import HTTPException as _HTTPException
                raise _HTTPException(status_code=422, detail=detail)
        # ─────────────────────────────────────────────────────────────────────

        # Build an absolute per-unit timeline directly from forced-aligner outputs.
        # Segment boundaries are textual only; timing always comes from unit start/end.
        need_trad_display = (
            mode == "auto_asr"
            or language_name == "Chinese"
            or any(_has_cjk(w.get("text")) for w in words)
        )
        units: List[Dict[str, Any]] = []
        audio_end = 0.0
        for w in words:
            raw_txt = str(w.get("text") or "").strip()
            if not raw_txt:
                continue
            txt = _to_traditional_for_display(raw_txt) if need_trad_display else raw_txt
            clean_len = len(_clean_display_text(txt))
            if clean_len <= 0:
                continue
            s = float(w.get("start") or 0.0)
            e = float(w.get("end") or s)
            if e <= s:
                e = s + 1e-3
            units.append({
                "text": txt,
                "start": s,
                "end": e,
                "clean_len": clean_len,
            })
            audio_end = max(audio_end, e)

        if not units:
            raise RuntimeError("alignment returned no valid timing units")

        provided_source_text = str(text or "").strip()
        if provided_source_text:
            # The visible subtitle text must be the user's script.  The forced
            # aligner/ASR text is only a timing source and may contain repeated
            # or normalized words, especially with cloned TTS audio.
            source_text = provided_source_text
        elif mode == "auto_asr":
            source_text = str(backend_text or "").strip()
            if need_trad_display:
                source_text = _to_traditional_for_display(source_text)
            if not _clean_display_text(source_text):
                source_text = _join_tokens([u["text"] for u in units])
        else:
            # User specifically requested: 如果有輸入演講稿 是用該演講稿為主 (Use provided text as primary reference)
            # Do NOT apply s2t conversion on user-provided script: the user already wrote the text
            # in the form they want displayed. Converting it would change their terminology
            # (e.g. 分布式 -> 分散式) which is undesirable.
            source_text = str(backend_text or "").strip()
            # Only apply trad conversion on ASR-derived fallback text, not user input.
            if source_text and need_trad_display:
                source_text = _to_traditional_for_display(source_text)

        repair_warning = ""
        repaired_ref_char_spans: Optional[List[Tuple[float, float]]] = None
        if mode == "scripted" and source_text:
            # Optional compatibility layer for numeric/unit alignments such as
            # "900px" or "1.23GB".  The display text stays unchanged, but a
            # second forced-alignment pass may use a spoken form
            # ("九百pixel") and map its timing back to the original text.
            repair_plan = make_repair_plan(source_text, units)
            if repair_plan.enabled and repair_plan.repaired_text and repair_plan.char_map:
                try:
                    repaired_words, repaired_backend, _ = _run_word_timestamp_worker(
                        runtime_python=runtime_python,
                        audio_path=audio_path,
                        language=language_name,
                        alignment_mode=mode,
                        text=repair_plan.repaired_text,
                        hf_token=hf_token,
                    )
                    repaired_units: List[Dict[str, Any]] = []
                    for rw in repaired_words:
                        raw_txt = str(rw.get("text") or "").strip()
                        if not raw_txt:
                            continue
                        txt = _to_traditional_for_display(raw_txt) if need_trad_display else raw_txt
                        s = float(rw.get("start") or 0.0)
                        e = float(rw.get("end") or s)
                        if e <= s:
                            e = s + 1e-3
                        repaired_units.append({"text": txt, "start": s, "end": e})
                    _, repaired_char_spans = _expand_units_to_char_spans(repaired_units)
                    mapped_spans = map_repaired_spans_to_source(
                        source_text,
                        repaired_char_spans,
                        repair_plan.char_map,
                    )
                    if len(mapped_spans) == len(_align_clean(source_text)):
                        repaired_ref_char_spans = mapped_spans
                        backend = f"{backend}+repair"
                        repair_warning = f"已對數字/單位異常時間軸啟用二次強對齊修正（{repaired_backend}）。"
                except Exception as exc:
                    repair_warning = f"數字/單位二次強對齊修正失敗，已保留原時間軸：{exc}"

        readable_chunks = _split_for_readability(source_text, min_chars=min_chars, max_chars=max_chars)
        if not readable_chunks:
            readable_chunks = [_join_tokens([u["text"] for u in units])]
        logger.info(
            "[SubtitleAlign] split min=%s max=%s mode=%s provided=%s source_preview=%r chunks=%s preview=%r",
            min_chars,
            max_chars,
            mode,
            bool(provided_source_text),
            source_text[:120],
            len(readable_chunks),
            readable_chunks[:4],
        )

        # ── ref_clean_text must use _align_clean (NOT _clean_display_text) ──────
        # _clean_display_text keeps punctuation, but _split_for_readability strips
        # punctuation from chunk boundaries. If ref_clean_text has punctuation, the
        # char_cursor drifts: each subsequent segment maps to timestamps that are
        # too early, making subtitles appear BEFORE the corresponding audio.
        # _align_clean strips BOTH spaces and punctuation, making cursor arithmetic
        # consistent with the punctuation-free chunks produced by _split_for_readability.
        ref_clean_text = _align_clean(source_text)
        asr_clean_text, asr_char_spans = _expand_units_to_char_spans(units)
        if not ref_clean_text:
            ref_clean_text = asr_clean_text
        if repaired_ref_char_spans is not None:
            ref_char_spans = repaired_ref_char_spans
            match_ratio = 1.0
        else:
            ref_char_spans, match_ratio = _align_reference_chars(ref_clean_text, asr_clean_text, asr_char_spans, audio_end)

        # ── 強對齊失敗檢測（Phase 2：文本相似度檢查） ──────────────────────────
        # 在 scripted 模式下，如果 match_ratio 過低代表文稿與音頻幾乎不對應，
        # 繼續強行對齊只會產生完全錯誤的時間軸，應直接告知使用者錯誤。
        warning_msg = ""
        if mode == "scripted" and match_ratio < 0.35 and len(ref_clean_text) > 5:
            detail = (
                f"⚠️ 強對齊失敗：講稿內容與音頻不符（文字相似度 {match_ratio*100:.0f}%）。\n"
                f"請確認您填入的講稿是否對應這段語音，"
                f"或將文稿欄位留空以改用 ASR 自動辨識。"
            )
            from fastapi import HTTPException as _HTTPException
            raise _HTTPException(status_code=422, detail=detail)
        elif match_ratio < 0.55:
            warning_msg = (
                f"⚠️ 系統提示：講稿與音頻的對應程度偏低（相似度 {match_ratio*100:.0f}%），"
                f"字幕時間軸可能有部分落差。建議確認講稿文字是否完全對應此音頻。"
            )
        if repair_warning:
            warning_msg = f"{warning_msg}\n{repair_warning}".strip()
        # ────────────────────────────────────────────────────────────────────

        segments: List[Dict[str, Any]] = []
        char_cursor = 0
        for chunk in readable_chunks:
            # Use _align_clean for cursor: punctuation-free count must match
            # the span array which is also indexed by _align_clean characters.
            chunk_align = _align_clean(chunk)
            clen = len(chunk_align)
            if clen <= 0:
                continue
            start_idx = char_cursor
            end_idx = min(len(ref_char_spans) - 1, start_idx + clen - 1)
            if end_idx < start_idx:
                continue
            seg_spans = ref_char_spans[start_idx:end_idx + 1]
            if not seg_spans:
                continue

            seg_text = str(chunk or "").strip() or _align_clean(chunk or "")
            start = float(seg_spans[0][0])
            end = float(seg_spans[-1][1])
            if end <= start:
                end = start + 0.25
            cspans_words = _build_display_words(seg_text, seg_spans)
            if not cspans_words:
                cspans_words = _build_display_words_fallback(seg_text, seg_spans)
            segments.append({
                "start": start,
                "end": end,
                "text": seg_text,
                "words": cspans_words,
            })
            char_cursor = end_idx + 1
            if char_cursor >= len(ref_char_spans):
                break

        # If chunking/text rules left trailing characters, append them to the last segment.
        if segments and char_cursor < len(ref_char_spans):
            tail_spans = ref_char_spans[char_cursor:]
            # ref_clean_text is now _align_clean (no punctuation), so just mark tail end.
            tail_end = float(tail_spans[-1][1])
            segments[-1]["end"] = max(float(segments[-1]["end"]), tail_end)
            last_words = segments[-1].get("words") or []
            if last_words:
                last_words[-1]["end"] = max(float(last_words[-1].get("end") or 0.0), tail_end)

        # Use strict word boundary times as requested, with duration smoothing
        for i_seg, seg in enumerate(segments):
            # Insert spacing between CJK and ASCII
            original_text = seg["text"]
            spaced_text = re.sub(r'([\u3400-\u9fff])([A-Za-z0-9])', r'\1 \2', original_text)
            spaced_text = re.sub(r'([A-Za-z0-9])([\u3400-\u9fff])', r'\1 \2', spaced_text)
            seg["text"] = spaced_text

            words = seg.get("words")
            if words:
                # 1. Borrowing time logic for short words
                min_dur = 0.08
                for i in range(len(words) - 1):
                    s_i, e_i = float(words[i].get("start", 0)), float(words[i].get("end", 0))
                    dur = e_i - s_i
                    if dur < min_dur:
                        deficit = min_dur - dur
                        s_next, e_next = float(words[i+1].get("start", 0)), float(words[i+1].get("end", 0))
                        next_dur = e_next - s_next
                        if next_dur > min_dur:
                            borrow = min(deficit, next_dur - min_dur)
                            words[i]["end"] = e_i + borrow
                            words[i+1]["start"] = s_next + borrow

                for i in range(len(words) - 1, 0, -1):
                    s_i, e_i = float(words[i].get("start", 0)), float(words[i].get("end", 0))
                    dur = e_i - s_i
                    if dur < min_dur:
                        deficit = min_dur - dur
                        s_prev, e_prev = float(words[i-1].get("start", 0)), float(words[i-1].get("end", 0))
                        prev_dur = e_prev - s_prev
                        if prev_dur > min_dur:
                            borrow = min(deficit, prev_dur - min_dur)
                            words[i]["start"] = s_i - borrow
                            words[i-1]["end"] = e_prev - borrow

                # 2. Fix overlaps and ensure monotonic
                for i in range(1, len(words)):
                    if float(words[i]["start"]) < float(words[i-1]["end"]):
                        mid = (float(words[i]["start"]) + float(words[i-1]["end"])) / 2
                        words[i-1]["end"] = mid
                        words[i]["start"] = mid

                # 3. Add small linger at the very end of the segment
                linger_time = 0.2
                new_tail = float(words[-1]["end"]) + linger_time
                if i_seg < len(segments) - 1:
                    next_start = float(segments[i_seg+1].get("start", new_tail))
                    new_tail = min(new_tail, next_start - 0.01) # leave 10ms gap
                words[-1]["end"] = new_tail

                for w in words:
                    wt = str(w.get("text", ""))
                    swt = re.sub(r'([\u3400-\u9fff])([A-Za-z0-9])', r'\1 \2', wt)
                    swt = re.sub(r'([A-Za-z0-9])([\u3400-\u9fff])', r'\1 \2', swt)
                    w["text"] = swt
                
                seg["start"] = float(words[0].get("start", seg["start"]))
                seg["end"] = max(seg["start"], float(words[-1].get("end", seg["end"])))
            else:
                seg["start"] = max(0.0, float(seg["start"]))
                seg["end"] = max(seg["start"], float(seg["end"]))

        if segments:
            last_end = segments[-1]["end"]
            audio_duration = max(audio_end, last_end)
        else:
            audio_duration = max(audio_end, 0.1)

        srt = _build_srt(segments)
        return AlignmentResult(
            segments=segments,
            srt=srt,
            backend=backend,
            audio_duration=float(audio_duration),
            warning=warning_msg,
            readable_chunks=readable_chunks,
            source_text=source_text,
        )
    finally:
        try:
            os.remove(audio_path)
        except Exception:
            pass
