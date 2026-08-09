import json
import logging
import os
import re
import subprocess
import tempfile
import textwrap
import threading
import uuid
import signal
import shutil
from collections import deque
import numpy as np
import soundfile as sf
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple


logger = logging.getLogger("video_abstract")
PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_MODELS_DIR = Path(os.getenv("SLIDEAI_MODELS_DIR", PROJECT_ROOT / "models"))
DEFAULT_RUNTIMES_DIR = PROJECT_ROOT / ".runtimes"

DEFAULT_VOX_TEXT_SPLIT_MODE = os.getenv("VOXTTS_TEXT_SPLIT_MODE", "per_4_sentences").strip().lower()
DEFAULT_VOX_EDGE_SILENCE_MS = float(os.getenv("VOXTTS_EDGE_SILENCE_MS", "120"))


def _to_bool(value: Optional[str], default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class VoxTTSConfig:
    enabled: bool
    model_path: str
    runtime_python: str
    optimize: bool
    load_denoiser: bool
    denoise_reference: bool
    zipenhancer_model_path: str
    infer_timeout_sec: int
    asr_runtime_python: str
    asr_model_path: str
    asr_timeout_sec: int
    text_split_mode: str
    edge_silence_ms: float
    engine: str
    nano_worker_path: str
    nano_timesteps: int
    nano_idle_timeout_sec: int
    nano_gpu_memory_utilization: float


def load_voxtts_config() -> VoxTTSConfig:
    return VoxTTSConfig(
        enabled=_to_bool(os.getenv("VOXTTS_ENABLED"), True),
        model_path=os.getenv(
            "VOXTTS_MODEL_PATH",
            str(DEFAULT_MODELS_DIR / "tts" / "VoxCPM2"),
        ).strip(),
        runtime_python=os.getenv(
            "VOXTTS_RUNTIME_PYTHON",
            str(DEFAULT_RUNTIMES_DIR / "voxcpm" / ".venv" / "bin" / "python"),
        ).strip(),
        optimize=_to_bool(os.getenv("VOXTTS_OPTIMIZE"), False),
        # ZipEnhancer is an optional ModelScope model, not part of VoxCPM2.
        # Keep it off by default so the first TTS request never performs a
        # hidden network download.  This also matches the official WebUI's
        # default for prompt-audio denoising.
        load_denoiser=_to_bool(os.getenv("VOXTTS_ENABLE_DENOISER"), False),
        denoise_reference=_to_bool(os.getenv("VOXTTS_DENOISE_REFERENCE"), False),
        zipenhancer_model_path=os.getenv(
            "VOXTTS_ZIPENHANCER_MODEL_PATH",
            "iic/speech_zipenhancer_ans_multiloss_16k_base",
        ).strip(),
        infer_timeout_sec=int(os.getenv("VOXTTS_INFER_TIMEOUT_SEC", "600")),
        asr_runtime_python=os.getenv(
            "QWEN3_ASR_RUNTIME_PYTHON",
            str(DEFAULT_RUNTIMES_DIR / "qwen-speech" / ".venv" / "bin" / "python"),
        ).strip(),
        asr_model_path=os.getenv(
            "QWEN3_ASR_MODEL_PATH",
            str(DEFAULT_MODELS_DIR / "asr" / "Qwen3-ASR-1.7B"),
        ).strip(),
        asr_timeout_sec=int(os.getenv("QWEN3_ASR_TIMEOUT_SEC", "600")),
        text_split_mode=DEFAULT_VOX_TEXT_SPLIT_MODE,
        edge_silence_ms=DEFAULT_VOX_EDGE_SILENCE_MS,
        engine=os.getenv("VOXTTS_ENGINE", "original").strip().lower(),
        nano_worker_path=os.getenv(
            "VOXTTS_NANO_WORKER_PATH",
            str(PROJECT_ROOT / "backend" / "app" / "workers" / "nano_voxcpm_worker.py"),
        ).strip(),
        nano_timesteps=int(os.getenv("VOXTTS_NANO_TIMESTEPS", "12")),
        nano_idle_timeout_sec=max(0, int(os.getenv("VOXTTS_NANO_IDLE_TIMEOUT_SEC", "120"))),
        nano_gpu_memory_utilization=float(os.getenv("VOXTTS_NANO_GPU_MEMORY_UTILIZATION", "0.50")),
    )


_CJK_SENTENCE_END_CHARS = "，,。！？!?；;…\n"
_ENGLISH_ABBREVIATIONS = {"e.g", "i.e", "u.s", "u.k", "dr", "mr", "mrs", "ms", "prof", "sr", "jr", "vs", "etc", "fig", "no", "inc", "ltd"}


def _is_english_sentence_period(text: str, index: int) -> bool:
    """Treat only a real English sentence-final dot as a boundary.

    Decimal values, versions, IP addresses and common abbreviations must stay
    in the same TTS chunk.  A normal English full stop is followed by spacing,
    Chinese text, or end-of-text, unlike internal dots in those tokens.
    """
    if index + 1 < len(text):
        next_char = text[index + 1]
        is_cjk_next = "\u3400" <= next_char <= "\u9fff"
        if not next_char.isspace() and not is_cjk_next:
            return False
    before = text[:index]
    token_match = re.search(r"([A-Za-z]+(?:\.[A-Za-z]+)*)$", before)
    if token_match and token_match.group(1).lower() in _ENGLISH_ABBREVIATIONS:
        return False
    if index > 0 and text[index - 1].isdigit():
        # A numeric token ending in '.' is ambiguous, but keep list/version
        # tokens intact; Chinese full stops remain an unambiguous boundary.
        numeric = re.search(r"\d+(?:\.\d+)+$", before)
        if numeric:
            return False
    return True


def split_text_into_sentences(text: str) -> List[str]:
    text = (text or "").strip()
    if not text:
        return []

    segments: List[str] = []
    buffer: List[str] = []
    for index, ch in enumerate(text):
        buffer.append(ch)
        if ch in _CJK_SENTENCE_END_CHARS or (ch == "." and _is_english_sentence_period(text, index)):
            sentence = "".join(buffer).strip()
            if sentence:
                segments.append(sentence)
            buffer = []

    if buffer:
        sentence = "".join(buffer).strip()
        if sentence:
            segments.append(sentence)

    return segments


def build_voxtts_text_chunks(text: str, split_mode: str = DEFAULT_VOX_TEXT_SPLIT_MODE) -> List[str]:
    raw_text = (text or "").strip()
    if not raw_text:
        return []

    if split_mode == "none":
        return [raw_text]

    sentences = split_text_into_sentences(raw_text)
    if not sentences:
        return [raw_text]

    if split_mode in {"per_sentence", "sentence"}:
        return sentences

    if split_mode in {"per_2_sentences", "per_2_sentence", "2", "two"}:
        return [" ".join(sentences[i : i + 2]).strip() for i in range(0, len(sentences), 2)]

    if split_mode in {"per_4_sentences", "per_4_sentence", "4", "four"}:
        return [" ".join(sentences[i : i + 4]).strip() for i in range(0, len(sentences), 4)]

    return [raw_text]


def _check_voxtts_ready(config: VoxTTSConfig) -> Tuple[bool, str]:
    if not config.enabled:
        return False, "VoxTTS is disabled"
    if not config.runtime_python or not os.path.isfile(config.runtime_python):
        return False, f"runtime python not found: {config.runtime_python}"
    if not config.model_path or not os.path.isdir(config.model_path):
        return False, f"model path not found: {config.model_path}"
    if not os.path.isfile(os.path.join(config.model_path, "config.json")):
        return False, f"model config missing: {config.model_path}"
    if config.engine == "nano_vllm" and not os.path.isfile(config.nano_worker_path):
        return False, f"Nano-vLLM worker not found: {config.nano_worker_path}"
    return True, "ok"


_NANO_WORKER: Optional[subprocess.Popen] = None
_NANO_WORKER_LOCK = threading.RLock()
_NANO_IDLE_TIMER: Optional[threading.Timer] = None
_NANO_WORKER_STDERR: deque[str] = deque(maxlen=40)


def _drain_nano_worker_stderr(proc: subprocess.Popen) -> None:
    if not proc.stderr:
        return
    for line in proc.stderr:
        text = str(line or "").strip()
        if text:
            _NANO_WORKER_STDERR.append(text)
            logger.debug("[Nano-VLLM VoxCPM][worker] %s", text)


def _stop_nano_worker() -> None:
    global _NANO_WORKER, _NANO_IDLE_TIMER
    with _NANO_WORKER_LOCK:
        if _NANO_IDLE_TIMER:
            _NANO_IDLE_TIMER.cancel()
            _NANO_IDLE_TIMER = None
        if _NANO_WORKER and _NANO_WORKER.poll() is None:
            # Nano-vLLM creates a GPU server child; terminate the isolated
            # process group so the child cannot retain CUDA memory after idle.
            os.killpg(_NANO_WORKER.pid, signal.SIGTERM)
        _NANO_WORKER = None


def release_voxtts_worker() -> None:
    """Immediately release Nano TTS GPU memory for the next pipeline stage."""
    _stop_nano_worker()


def _release_competing_speech_workers() -> None:
    """Unload the previous speech stage before allocating Nano/ASR VRAM."""
    for module_name, function_name in (
        ("backend.app.services.qwen3_tts", "release_qwen3_tts_worker"),
        ("backend.app.services.subtitle_alignment", "release_alignment_worker"),
    ):
        try:
            module = __import__(module_name, fromlist=[function_name])
            getattr(module, function_name)()
        except Exception as exc:
            logger.warning("[Speech lifecycle] could not release competing worker: %s", exc)


def _cancel_nano_idle_shutdown_locked() -> None:
    global _NANO_IDLE_TIMER
    if _NANO_IDLE_TIMER:
        _NANO_IDLE_TIMER.cancel()
        _NANO_IDLE_TIMER = None


def _schedule_nano_idle_shutdown(config: VoxTTSConfig) -> None:
    global _NANO_IDLE_TIMER
    if config.nano_idle_timeout_sec <= 0:
        return
    with _NANO_WORKER_LOCK:
        _cancel_nano_idle_shutdown_locked()
        _NANO_IDLE_TIMER = threading.Timer(config.nano_idle_timeout_sec, _stop_nano_worker)
        _NANO_IDLE_TIMER.daemon = True
        _NANO_IDLE_TIMER.start()
        logger.info("[Nano-VLLM VoxCPM] idle shutdown scheduled in %ss", config.nano_idle_timeout_sec)


def _ensure_nano_worker(config: VoxTTSConfig) -> subprocess.Popen:
    global _NANO_WORKER
    _cancel_nano_idle_shutdown_locked()
    if _NANO_WORKER and _NANO_WORKER.poll() is None:
        return _NANO_WORKER
    _NANO_WORKER_STDERR.clear()
    _NANO_WORKER = subprocess.Popen(
        [config.runtime_python, config.nano_worker_path, config.model_path, str(config.nano_timesteps), str(config.nano_gpu_memory_utilization)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1, start_new_session=True,
    )
    threading.Thread(target=_drain_nano_worker_stderr, args=(_NANO_WORKER,), daemon=True).start()
    ready = _NANO_WORKER.stdout.readline().strip() if _NANO_WORKER.stdout else ""
    if not ready.startswith("__NANO_VOX_RESULT__"):
        detail = " | ".join(_NANO_WORKER_STDERR)[-1600:]
        _stop_nano_worker()
        if "out of memory" in detail.lower() or "cuda" in detail.lower():
            raise RuntimeError(f"Nano-vLLM worker failed to become ready (GPU/CUDA): {detail}")
        raise RuntimeError(f"Nano-vLLM worker failed to become ready{f': {detail}' if detail else ''}")
    return _NANO_WORKER


def _run_nanovllm_infer(*, config: VoxTTSConfig, text: str, output_path: str, reference_audio_path: Optional[str], auxiliary_text: str) -> Tuple[bool, Optional[str], str]:
    if not reference_audio_path or not auxiliary_text.strip():
        return False, None, "Nano-VLLM VoxCPM requires reference audio and its transcript for ultimate voice cloning"
    _release_competing_speech_workers()
    with _NANO_WORKER_LOCK:
        try:
            worker = _ensure_nano_worker(config)
            if not worker.stdin or not worker.stdout:
                raise RuntimeError("Nano-vLLM worker pipes unavailable")
            request_id = uuid.uuid4().hex
            worker.stdin.write(json.dumps({"id": request_id, "text": text, "output_path": output_path, "reference_audio_path": reference_audio_path, "prompt_text": auxiliary_text, "cfg_value": 2.0, "inference_timesteps": config.nano_timesteps}, ensure_ascii=False) + "\n")
            worker.stdin.flush()
            prefix = "__NANO_VOX_RESULT__"
            while True:
                line = worker.stdout.readline()
                if not line:
                    _stop_nano_worker()
                    raise RuntimeError("Nano-vLLM worker exited during synthesis")
                if line.startswith(prefix):
                    result = json.loads(line[len(prefix):])
                    if result.get("id") == request_id:
                        break
            if not result.get("ok"):
                return False, None, str(result.get("error") or "Nano-vLLM synthesis failed")[:1200]
            return True, output_path, "ok"
        except Exception as exc:
            return False, None, str(exc)


def _run_nanovllm_chunked_infer(*, config: VoxTTSConfig, text: str, output_path: str, reference_audio_path: Optional[str], auxiliary_text: str) -> Tuple[bool, Optional[str], str]:
    chunks = build_voxtts_text_chunks(text, config.text_split_mode)
    if not chunks:
        return False, None, "no text chunks generated"
    if len(chunks) == 1:
        result = _run_nanovllm_infer(config=config, text=chunks[0], output_path=output_path, reference_audio_path=reference_audio_path, auxiliary_text=auxiliary_text)
        if result[0] and os.path.isfile(output_path):
            _persist_chunk_artifacts(
                output_path=output_path,
                chunks=chunks,
                part_paths=[output_path],
                silence_ms=0.0,
            )
        _schedule_nano_idle_shutdown(config)
        return result

    part_paths: list[str] = []
    try:
        for index, chunk in enumerate(chunks, start=1):
            with tempfile.NamedTemporaryFile(delete=False, suffix=f".part{index:03d}.wav") as part_fp:
                part_path = part_fp.name
            part_paths.append(part_path)
            ok, _, reason = _run_nanovllm_infer(
                config=config, text=chunk, output_path=part_path,
                reference_audio_path=reference_audio_path, auxiliary_text=auxiliary_text,
            )
            if not ok:
                return False, None, f"segment {index}/{len(chunks)} failed: {reason}"

        audio_parts, sample_rate = [], None
        silence_ms = max(0.0, config.edge_silence_ms)
        for index, part_path in enumerate(part_paths):
            wav, rate = sf.read(part_path, dtype="float32")
            if sample_rate is None:
                sample_rate = rate
            if rate != sample_rate:
                return False, None, f"sample-rate mismatch in segment {index + 1}"
            audio_parts.append(wav)
            if index < len(part_paths) - 1 and silence_ms:
                audio_parts.append(np.zeros(int(sample_rate * silence_ms / 1000.0), dtype=np.float32))
        sf.write(output_path, np.concatenate(audio_parts), sample_rate)
        _persist_chunk_artifacts(
            output_path=output_path,
            chunks=chunks,
            part_paths=part_paths,
            silence_ms=silence_ms,
        )
        logger.info("[Nano-VLLM VoxCPM] generated %d punctuation chunks", len(chunks))
        return True, output_path, "ok"
    finally:
        _schedule_nano_idle_shutdown(config)
        for part_path in part_paths:
            try:
                os.remove(part_path)
            except OSError:
                pass


def _persist_chunk_artifacts(
    *,
    output_path: str,
    chunks: List[str],
    part_paths: List[str],
    silence_ms: float,
) -> None:
    """Keep deterministic four-sentence boundaries for later local regeneration."""
    chunks_dir = Path(output_path).with_suffix(".chunks")
    shutil.rmtree(chunks_dir, ignore_errors=True)
    chunks_dir.mkdir(parents=True, exist_ok=True)
    timeline = []
    cursor = 0.0
    for index, (text, part_path) in enumerate(zip(chunks, part_paths)):
        info = sf.info(part_path)
        duration = float(info.frames) / float(info.samplerate or 1)
        filename = f"chunk_{index + 1:03d}.wav"
        destination = chunks_dir / filename
        if Path(part_path).resolve() != destination.resolve():
            shutil.copy2(part_path, destination)
        timeline.append({
            "index": index,
            "text": text,
            "start": round(cursor, 6),
            "end": round(cursor + duration, 6),
            "duration": round(duration, 6),
            "filename": filename,
        })
        cursor += duration
        if index < len(chunks) - 1:
            cursor += max(0.0, silence_ms) / 1000.0
    (chunks_dir / "chunks.json").write_text(
        json.dumps({"silence_ms": silence_ms, "chunks": timeline}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _build_vox_final_text(text: str, control_instruction: str) -> str:
    control = (control_instruction or "").strip()
    if not control:
        return text
    control = control.replace("(", "").replace(")", "").replace("（", "").replace("）", "").strip()
    return f"({control}){text}"


def get_voxtts_generation_settings() -> dict:
    config = load_voxtts_config()
    return {
        "text_split_mode": config.text_split_mode,
        "edge_silence_ms": config.edge_silence_ms,
        "split_sentence_count": 2 if config.text_split_mode == "per_2_sentences" else 4,
    }


def _py_literal(value: object) -> str:
    return repr(value)


def _run_voxtts_infer(
    *,
    text: str,
    output_path: str,
    reference_audio_path: Optional[str],
    auxiliary_text: str,
    use_prompt_text: bool,
) -> Tuple[bool, Optional[str], str]:
    config = load_voxtts_config()
    ok, reason = _check_voxtts_ready(config)
    if not ok:
        return False, None, reason

    output_path = os.path.abspath(output_path)
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    reference_audio_path = os.path.abspath(reference_audio_path) if reference_audio_path else None

    final_text = text if use_prompt_text else _build_vox_final_text(text, auxiliary_text)

    if config.engine == "nano_vllm":
        return _run_nanovllm_chunked_infer(
            config=config, text=final_text, output_path=output_path,
            reference_audio_path=reference_audio_path, auxiliary_text=auxiliary_text,
        )

    chunks = build_voxtts_text_chunks(final_text, config.text_split_mode)
    if not chunks:
        return False, None, "no text chunks generated"
    part_paths: list[str] = []
    for index in range(len(chunks)):
        with tempfile.NamedTemporaryFile(delete=False, suffix=f".part{index + 1:03d}.wav") as part_fp:
            part_paths.append(part_fp.name)
    silence_ms = max(0.0, config.edge_silence_ms)
    script = textwrap.dedent(f"""
import json
import numpy as np
import soundfile as sf
from voxcpm import VoxCPM

model = VoxCPM.from_pretrained(
    {_py_literal(config.model_path)},
    load_denoiser={_py_literal(config.load_denoiser)},
    zipenhancer_model_id={_py_literal(config.zipenhancer_model_path)},
    optimize={_py_literal(config.optimize)},
)

kwargs = dict(
    cfg_value=2.0,
    inference_timesteps={_py_literal(config.nano_timesteps)},
    normalize=False,
    denoise={_py_literal(config.load_denoiser and config.denoise_reference)},
)

reference_audio = {_py_literal(reference_audio_path)}
aux_text = {_py_literal(auxiliary_text)}
use_prompt = {_py_literal(bool(use_prompt_text))}

if reference_audio:
    kwargs["reference_wav_path"] = reference_audio
if use_prompt and reference_audio:
    kwargs["prompt_wav_path"] = reference_audio
    kwargs["prompt_text"] = aux_text

texts = {_py_literal(chunks)}
part_paths = {_py_literal(part_paths)}
silence_ms = {_py_literal(silence_ms)}
parts = []
for index, chunk in enumerate(texts):
    kwargs["text"] = chunk
    wav = model.generate(**kwargs)
    sf.write(part_paths[index], wav, model.tts_model.sample_rate)
    parts.append(wav)
    if index < len(texts) - 1 and silence_ms > 0:
        parts.append(np.zeros(int(model.tts_model.sample_rate * silence_ms / 1000.0), dtype=np.float32))
sf.write({_py_literal(output_path)}, np.concatenate(parts), model.tts_model.sample_rate)
print(json.dumps({{"output_path": {_py_literal(output_path)}}}, ensure_ascii=False))
""")
    _release_competing_speech_workers()
    try:
        proc = subprocess.run(
            [config.runtime_python, "-c", script],
            capture_output=True,
            text=True,
            timeout=config.infer_timeout_sec,
        )
    except Exception as exc:
        for part_path in part_paths:
            Path(part_path).unlink(missing_ok=True)
        return False, None, str(exc)
    if proc.returncode != 0:
        msg = (proc.stderr or proc.stdout or "voxtts infer failed").strip()
        for part_path in part_paths:
            Path(part_path).unlink(missing_ok=True)
        # Tracebacks and download errors are normally at the end.  Returning
        # the prefix hid the actual failure behind progress-bar output.
        if len(msg) > 2400:
            msg = "…\n" + msg[-2400:]
        return False, None, msg
    if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
        for part_path in part_paths:
            Path(part_path).unlink(missing_ok=True)
        return False, None, "voxtts finished but no audio produced"
    try:
        _persist_chunk_artifacts(
            output_path=output_path,
            chunks=chunks,
            part_paths=part_paths,
            silence_ms=silence_ms,
        )
    finally:
        for part_path in part_paths:
            Path(part_path).unlink(missing_ok=True)
    return True, output_path, "ok"


def synthesize_voxtts_preview(
    *,
    text: str,
    reference_audio_bytes: Optional[bytes],
    reference_suffix: str = ".wav",
    auxiliary_text: str = "",
    use_prompt_text: bool = False,
) -> Tuple[bool, Optional[str], str]:
    ref_tmp = None
    out_wav = None
    try:
        if reference_audio_bytes:
            with tempfile.NamedTemporaryFile(delete=False, suffix=reference_suffix or ".wav") as ref_fp:
                ref_fp.write(reference_audio_bytes)
                ref_tmp = ref_fp.name
        with tempfile.NamedTemporaryFile(delete=False, suffix=".wav") as out_fp:
            out_wav = out_fp.name
        return _run_voxtts_infer(
            text=text,
            output_path=out_wav,
            reference_audio_path=ref_tmp,
            auxiliary_text=auxiliary_text,
            use_prompt_text=use_prompt_text,
        )
    finally:
        if ref_tmp and os.path.exists(ref_tmp):
            try:
                os.remove(ref_tmp)
            except Exception:
                pass


def synthesize_voxtts_to_file(
    text: str,
    output_path: str,
    *,
    reference_audio_path: Optional[str] = None,
    auxiliary_text: str = "",
    use_prompt_text: bool = False,
) -> Tuple[bool, Optional[str], str]:
    return _run_voxtts_infer(
        text=text,
        output_path=output_path,
        reference_audio_path=reference_audio_path,
        auxiliary_text=auxiliary_text,
        use_prompt_text=use_prompt_text,
    )


def transcribe_with_local_qwen3_asr(
    reference_audio_bytes: bytes,
    reference_suffix: str = ".wav",
) -> Tuple[bool, str, str]:
    config = load_voxtts_config()
    if not config.asr_runtime_python or not os.path.isfile(config.asr_runtime_python):
        return False, "", f"ASR runtime python not found: {config.asr_runtime_python}"
    if not config.asr_model_path or not os.path.isdir(config.asr_model_path):
        return False, "", f"ASR model path not found: {config.asr_model_path}"

    _release_competing_speech_workers()
    ref_tmp = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=reference_suffix or ".wav") as ref_fp:
            ref_fp.write(reference_audio_bytes)
            ref_tmp = ref_fp.name
        script = f"""
import json
import torch
from qwen_asr import Qwen3ASRModel

model = Qwen3ASRModel.from_pretrained(
    {json.dumps(config.asr_model_path)},
    device_map={json.dumps(os.getenv("QWEN3_ASR_DEVICE", "cuda:0"))},
    dtype=getattr(torch, {json.dumps(os.getenv("QWEN3_ASR_DTYPE", "bfloat16"))}, torch.bfloat16),
    attn_implementation={json.dumps(os.getenv("QWEN3_ASR_ATTN_IMPL", "eager"))},
)
result = model.transcribe(
    audio={json.dumps(ref_tmp)},
    language=None,
    return_time_stamps=False,
)[0]
print(json.dumps({{"text": str(getattr(result, "text", "") or "")}}, ensure_ascii=False))
"""
        proc = subprocess.run(
            [config.asr_runtime_python, "-c", script],
            capture_output=True,
            text=True,
            timeout=config.asr_timeout_sec,
        )
        if proc.returncode != 0:
            msg = (proc.stderr or proc.stdout or "qwen3 asr failed").strip()
            return False, "", msg[:1200]
        raw_out = (proc.stdout or "").strip()
        payload = None
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
            return False, "", "ASR returned invalid output"
        return True, str(payload.get("text") or "").strip(), "ok"
    except Exception as exc:
        return False, "", str(exc)
    finally:
        if ref_tmp and os.path.exists(ref_tmp):
            try:
                os.remove(ref_tmp)
            except Exception:
                pass
