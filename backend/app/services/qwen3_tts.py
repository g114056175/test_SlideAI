import json
import logging
import os
import subprocess
import tempfile
import threading
import time
import uuid
import hashlib
import select
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple


logger = logging.getLogger("video_abstract")
_DOWNLOAD_LOCK = threading.Lock()
_WORKER_LOCK = threading.RLock()
_WORKER_PROC = None
_WORKER_CONFIG_KEY = None
_WORKER_IDLE_TIMER = None
_REF_CACHE = {}
_OPENCC_CONVERTER = None
_OPENCC_IMPORT_FAILED = False


def _to_bool(value: str, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _to_simplified_chinese_for_qwen(text: str) -> str:
    """
    Convert Traditional Chinese to Simplified Chinese before sending text to Qwen TTS.
    This affects Qwen input only; original script/subtitle text is unchanged.
    """
    global _OPENCC_CONVERTER, _OPENCC_IMPORT_FAILED
    if not text:
        return text
    if _OPENCC_IMPORT_FAILED:
        logger.debug("[Qwen3 TTS] opencc 不可用，跳過繁→簡轉換，原始文字: %r", text[:80])
        return text
    if _OPENCC_CONVERTER is None:
        try:
            from opencc import OpenCC  # type: ignore
            _OPENCC_CONVERTER = OpenCC("t2s")
        except Exception as exc:
            _OPENCC_IMPORT_FAILED = True
            logger.warning(
                "[Qwen3 TTS] opencc unavailable; skip Traditional->Simplified conversion: %s",
                exc,
            )
            return text
    try:
        converted = _OPENCC_CONVERTER.convert(text)
        if converted != text:
            logger.debug("[Qwen3 TTS] 繁→簡 轉換: %r → %r", text[:80], converted[:80])
        else:
            logger.debug("[Qwen3 TTS] 繁→簡 無變化: %r", text[:80])
        return converted
    except Exception as exc:
        logger.warning("[Qwen3 TTS] opencc conversion failed; fallback original text: %s", exc)
        return text


@dataclass(frozen=True)
class Qwen3TTSConfig:
    enabled: bool
    model_path: str
    model_id: str
    runtime_python: str
    auto_download: bool
    cache_dir: Optional[str]
    hf_token: Optional[str]
    device: str
    dtype: str
    attn_implementation: str
    language: str
    download_timeout_sec: int
    infer_timeout_sec: int
    worker_idle_timeout_sec: int


def load_qwen3_tts_config() -> Qwen3TTSConfig:
    project_root = Path(__file__).resolve().parents[3]
    models_dir = Path(os.getenv("SLIDEAI_MODELS_DIR", project_root / "models"))
    default_model_path = str(models_dir / "tts" / "Qwen3-TTS-12Hz-1.7B-Base")
    default_runtime_python = str(
        project_root / ".runtimes" / "qwen-speech" / ".venv" / "bin" / "python"
    )

    return Qwen3TTSConfig(
        enabled=_to_bool(os.getenv("QWEN3_TTS_ENABLED"), True),
        model_path=os.getenv("QWEN3_TTS_MODEL_PATH", default_model_path).strip(),
        model_id=os.getenv("QWEN3_TTS_MODEL_ID", "Qwen/Qwen3-TTS-12Hz-1.7B-Base").strip(),
        runtime_python=os.getenv("QWEN3_TTS_RUNTIME_PYTHON", default_runtime_python).strip(),
        auto_download=_to_bool(os.getenv("QWEN3_TTS_AUTO_DOWNLOAD"), True),
        cache_dir=(os.getenv("QWEN3_TTS_CACHE_DIR") or "").strip() or None,
        hf_token=(os.getenv("HF_TOKEN") or os.getenv("HUGGINGFACE_HUB_TOKEN") or "").strip() or None,
        device=os.getenv("QWEN3_TTS_DEVICE", "cuda:0").strip(),
        dtype=os.getenv("QWEN3_TTS_DTYPE", "bfloat16").strip(),
        attn_implementation=os.getenv("QWEN3_TTS_ATTN_IMPL", "eager").strip(),
        language=os.getenv("QWEN3_TTS_LANGUAGE", "auto").strip(),  # NOTE: 鎖定為 'auto'，不應覆寫
        download_timeout_sec=int(os.getenv("QWEN3_TTS_DOWNLOAD_TIMEOUT_SEC", "3600")),
        infer_timeout_sec=int(os.getenv("QWEN3_TTS_INFER_TIMEOUT_SEC", "180")),
        worker_idle_timeout_sec=int(os.getenv("QWEN3_TTS_WORKER_IDLE_TIMEOUT_SEC", "60")),
    )


def _is_model_ready(model_path: str) -> bool:
    return os.path.isdir(model_path) and os.path.isfile(os.path.join(model_path, "config.json"))


def _runtime_python_exists(runtime_python: str) -> bool:
    return bool(runtime_python) and os.path.isfile(runtime_python) and os.access(runtime_python, os.X_OK)


def ensure_model_available(config: Qwen3TTSConfig) -> Tuple[bool, str]:
    if not config.enabled:
        return False, "Qwen3 TTS is disabled"
    if not _runtime_python_exists(config.runtime_python):
        return False, f"runtime python not found: {config.runtime_python}"
    if _is_model_ready(config.model_path):
        return True, "model ready"
    if not config.auto_download:
        return False, f"model not found at {config.model_path} and auto download is disabled"

    with _DOWNLOAD_LOCK:
        if _is_model_ready(config.model_path):
            return True, "model ready"

        Path(config.model_path).parent.mkdir(parents=True, exist_ok=True)

        download_script = f"""
import os
from huggingface_hub import snapshot_download

repo_id = {json.dumps(config.model_id)}
local_dir = {json.dumps(config.model_path)}
token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACE_HUB_TOKEN") or None
snapshot_download(repo_id=repo_id, local_dir=local_dir, token=token)
print(local_dir)
"""

        env = os.environ.copy()
        if config.hf_token:
            env["HF_TOKEN"] = config.hf_token
            env["HUGGINGFACE_HUB_TOKEN"] = config.hf_token
        if config.cache_dir:
            env["HF_HOME"] = config.cache_dir
            env["HUGGINGFACE_HUB_CACHE"] = os.path.join(config.cache_dir, "hub")

        logger.info(
            "[Qwen3 TTS] model missing, downloading from %s to %s",
            config.model_id,
            config.model_path,
        )

        proc = subprocess.run(
            [config.runtime_python, "-c", download_script],
            capture_output=True,
            text=True,
            timeout=config.download_timeout_sec,
            env=env,
        )
        if proc.returncode != 0:
            msg = (proc.stderr or proc.stdout or "").strip()
            return False, f"download failed: {msg[:600]}"
        if not _is_model_ready(config.model_path):
            return False, "download completed but model files are incomplete"
        return True, "downloaded"


def _worker_config_key(config: Qwen3TTSConfig) -> tuple:
    return (
        config.runtime_python,
        config.model_path,
        config.device,
        config.dtype,
        config.attn_implementation,
    )


def _worker_cwd(runtime_python: str) -> Optional[str]:
    try:
        path = Path(runtime_python).resolve()
        if len(path.parents) >= 3:
            return str(path.parents[2])
    except Exception:
        pass
    return None


def _build_worker_script(config: Qwen3TTSConfig) -> str:
    return f"""
import json
import sys
import traceback
import torch
import soundfile as sf
from qwen_tts import Qwen3TTSModel

RESULT_PREFIX = "__QWEN3_TTS_RESULT__"

dtype_map = {{
    "float16": torch.float16,
    "fp16": torch.float16,
    "bfloat16": torch.bfloat16,
    "bf16": torch.bfloat16,
    "float32": torch.float32,
    "fp32": torch.float32,
}}
dtype = dtype_map.get({json.dumps(config.dtype)}.lower(), torch.bfloat16)

model = Qwen3TTSModel.from_pretrained(
    {json.dumps(config.model_path)},
    device_map={json.dumps(config.device)},
    dtype=dtype,
    attn_implementation={json.dumps(config.attn_implementation)},
)
try:
    model.eval()
except Exception:
    pass

print(RESULT_PREFIX + json.dumps({{"type": "ready"}}, ensure_ascii=False), flush=True)

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        job = json.loads(line)
        if job.get("cmd") == "shutdown":
            print(RESULT_PREFIX + json.dumps({{"type": "shutdown"}}, ensure_ascii=False), flush=True)
            break
        job_id = job.get("id")
        with torch.inference_mode():
            wavs, sr = model.generate_voice_clone(
                text=job.get("text") or "",
                language=job.get("language") or "auto",
                ref_audio=job.get("ref_audio") or "",
                ref_text=job.get("ref_text") or "",
                x_vector_only_mode=bool(job.get("x_vector_only_mode", True)),
            )
        sf.write(job.get("output_path"), wavs[0], sr)
        payload = {{"type": "result", "id": job_id, "ok": True, "sample_rate": sr}}
    except Exception as exc:
        payload = {{
            "type": "result",
            "id": locals().get("job", {{}}).get("id"),
            "ok": False,
            "error": str(exc),
            "traceback": traceback.format_exc()[-2000:],
        }}
    print(RESULT_PREFIX + json.dumps(payload, ensure_ascii=False), flush=True)
"""


def _drain_worker_stderr(proc: subprocess.Popen) -> None:
    try:
        for line in proc.stderr or []:
            text = line.rstrip()
            if text:
                logger.debug("[Qwen3 TTS worker stderr] %s", text)
    except Exception:
        pass


def _clear_ref_cache() -> None:
    global _REF_CACHE
    for path in list(_REF_CACHE.values()):
        try:
            if path and os.path.exists(path):
                os.remove(path)
        except Exception:
            pass
    _REF_CACHE = {}


def _stop_worker_locked() -> None:
    global _WORKER_PROC, _WORKER_CONFIG_KEY, _WORKER_IDLE_TIMER
    if _WORKER_IDLE_TIMER:
        try:
            _WORKER_IDLE_TIMER.cancel()
        except Exception:
            pass
        _WORKER_IDLE_TIMER = None
    proc = _WORKER_PROC
    _WORKER_PROC = None
    _WORKER_CONFIG_KEY = None
    if proc and proc.poll() is None:
        try:
            proc.stdin.write(json.dumps({"cmd": "shutdown"}) + "\n")
            proc.stdin.flush()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
    _clear_ref_cache()


def release_qwen3_tts_worker() -> None:
    """Immediately release Qwen3 TTS VRAM before ASR/alignment or another TTS."""
    with _WORKER_LOCK:
        _stop_worker_locked()


def _release_competing_speech_workers() -> None:
    """Keep speech models sequential so page-to-page work cannot stack VRAM."""
    for module_name, function_name in (
        ("backend.app.services.voxtts", "release_voxtts_worker"),
        ("backend.app.services.subtitle_alignment", "release_alignment_worker"),
    ):
        try:
            module = __import__(module_name, fromlist=[function_name])
            getattr(module, function_name)()
        except Exception as exc:
            logger.warning("[Qwen3 TTS worker] could not release competing worker: %s", exc)


def _schedule_worker_idle_shutdown_locked(config: Qwen3TTSConfig) -> None:
    global _WORKER_IDLE_TIMER
    if _WORKER_IDLE_TIMER:
        try:
            _WORKER_IDLE_TIMER.cancel()
        except Exception:
            pass
        _WORKER_IDLE_TIMER = None
    idle_sec = max(1, int(config.worker_idle_timeout_sec or 60))

    def _shutdown_if_idle() -> None:
        with _WORKER_LOCK:
            if _WORKER_PROC is None or _WORKER_PROC.poll() is not None:
                return
            logger.info("[Qwen3 TTS worker] idle for %ss, shutting down", idle_sec)
            _stop_worker_locked()

    _WORKER_IDLE_TIMER = threading.Timer(idle_sec, _shutdown_if_idle)
    _WORKER_IDLE_TIMER.daemon = True
    _WORKER_IDLE_TIMER.start()


def _read_worker_message(proc: subprocess.Popen, timeout_sec: int, expected_id: Optional[str] = None) -> dict:
    prefix = "__QWEN3_TTS_RESULT__"
    deadline = time.monotonic() + max(1, int(timeout_sec or 180))
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"qwen3 worker exited with code {proc.returncode}")
        ready, _, _ = select.select([proc.stdout], [], [], 0.1)
        if not ready:
            continue
        line = proc.stdout.readline()
        if not line:
            continue
        line = line.rstrip()
        if not line.startswith(prefix):
            logger.debug("[Qwen3 TTS worker stdout] %s", line)
            continue
        payload = json.loads(line[len(prefix):])
        if expected_id and payload.get("type") == "result" and payload.get("id") != expected_id:
            logger.debug("[Qwen3 TTS worker] skip stale result id=%s", payload.get("id"))
            continue
        return payload
    raise TimeoutError(f"qwen3 worker timed out after {timeout_sec}s")


def _ensure_worker_locked(config: Qwen3TTSConfig) -> subprocess.Popen:
    global _WORKER_PROC, _WORKER_CONFIG_KEY, _WORKER_IDLE_TIMER
    key = _worker_config_key(config)
    if _WORKER_IDLE_TIMER:
        try:
            _WORKER_IDLE_TIMER.cancel()
        except Exception:
            pass
        _WORKER_IDLE_TIMER = None
    if _WORKER_PROC and _WORKER_PROC.poll() is None and _WORKER_CONFIG_KEY == key:
        return _WORKER_PROC
    if _WORKER_PROC:
        _stop_worker_locked()

    logger.info("[Qwen3 TTS worker] starting persistent worker: model=%s", config.model_path)
    proc = subprocess.Popen(
        [config.runtime_python, "-u", "-c", _build_worker_script(config)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        cwd=_worker_cwd(config.runtime_python),
    )
    threading.Thread(target=_drain_worker_stderr, args=(proc,), daemon=True).start()
    ready = _read_worker_message(proc, config.infer_timeout_sec)
    if ready.get("type") != "ready":
        raise RuntimeError(f"qwen3 worker did not become ready: {ready}")
    _WORKER_PROC = proc
    _WORKER_CONFIG_KEY = key
    return proc


def _cache_reference_audio(reference_audio_bytes: bytes, reference_suffix: str) -> str:
    suffix = reference_suffix or ".wav"
    if not suffix.startswith("."):
        suffix = f".{suffix}"
    digest = hashlib.sha256(reference_audio_bytes + suffix.encode("utf-8", "ignore")).hexdigest()
    cached = _REF_CACHE.get(digest)
    if cached and os.path.exists(cached):
        return cached
    cache_dir = Path(tempfile.gettempdir()) / "slideai_qwen3_tts_ref_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / f"{digest}{suffix}"
    path.write_bytes(reference_audio_bytes)
    _REF_CACHE[digest] = str(path)
    return str(path)


def _synthesize_with_worker(
    config: Qwen3TTSConfig,
    *,
    text: str,
    ref_audio_path: str,
    output_path: str,
    language: str,
    ref_text: str,
    x_vector_only_mode: bool,
) -> Tuple[bool, Optional[str], str]:
    _release_competing_speech_workers()
    with _WORKER_LOCK:
        try:
            proc = _ensure_worker_locked(config)
            job_id = uuid.uuid4().hex
            payload = {
                "id": job_id,
                "text": text,
                "language": language or "auto",
                "ref_audio": ref_audio_path,
                "ref_text": ref_text or "",
                "x_vector_only_mode": bool(x_vector_only_mode),
                "output_path": output_path,
            }
            proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            proc.stdin.flush()
            result = _read_worker_message(proc, config.infer_timeout_sec, expected_id=job_id)
            if not result.get("ok"):
                return False, None, str(result.get("error") or "qwen3 worker failed")
            if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
                return False, None, "qwen3 worker finished but no audio produced"
            return True, output_path, "ok"
        except Exception as exc:
            logger.error("[Qwen3 TTS worker] synthesis failed: %s", exc, exc_info=True)
            _stop_worker_locked()
            return False, None, str(exc)
        finally:
            _schedule_worker_idle_shutdown_locked(config)


def warm_qwen3_tts_worker_async() -> None:
    """Start the persistent worker in the background without blocking the UI request."""
    config = load_qwen3_tts_config()
    ok, reason = ensure_model_available(config)
    if not ok:
        logger.warning("[Qwen3 TTS worker] warmup skipped: %s", reason)
        return

    def _warm() -> None:
        _release_competing_speech_workers()
        with _WORKER_LOCK:
            try:
                _ensure_worker_locked(config)
                _schedule_worker_idle_shutdown_locked(config)
            except Exception as exc:
                logger.warning("[Qwen3 TTS worker] warmup failed: %s", exc, exc_info=True)
                _stop_worker_locked()

    threading.Thread(target=_warm, daemon=True).start()


def synthesize_voice_clone_preview(
    text: str,
    reference_audio_bytes: bytes,
    reference_suffix: str = ".wav",
    ref_text: str = "",
    x_vector_only_mode: Optional[bool] = None,
) -> Tuple[bool, Optional[str], str]:
    config = load_qwen3_tts_config()
    ok, reason = ensure_model_available(config)
    if not ok:
        return False, None, reason

    ref_tmp = None
    out_wav = None
    try:
        qwen_text = _to_simplified_chinese_for_qwen(text)
        qwen_ref_text = _to_simplified_chinese_for_qwen(ref_text or "")

        # x_vector_only_mode：有 ref_text → ICL 模式（品質佳）；無 ref_text → x-vector only（安全回退）
        # 對齊官方 demo.py 做法，不使用環境變數預設
        if x_vector_only_mode is None:
            has_ref_text = bool((qwen_ref_text or "").strip())
            x_vector_only_mode = not has_ref_text   # 有文字 → False（ICL）；沒有 → True（x-vec）
            logger.debug(
                "[Qwen3 TTS] ref_text='%s' → x_vector_only_mode=%s",
                (qwen_ref_text or "")[:40],
                x_vector_only_mode,
            )
        # language 強制 Auto，讓模型自行偵測（對齊官方 demo）
        effective_language = "auto"
        logger.debug(
            "[Qwen3 TTS] language=%s, x_vector_only=%s, text=%r, ref_text=%r",
            effective_language, x_vector_only_mode, qwen_text[:60], qwen_ref_text[:60],
        )

        with tempfile.NamedTemporaryFile(delete=False, suffix=".wav") as out_fp:
            out_wav = out_fp.name

        # When using ICL mode, Qwen3 requires non-empty ref_text.
        if not x_vector_only_mode and not (qwen_ref_text or "").strip():
            return False, None, "ref_text is required when x_vector_only_mode=False"

        ref_tmp = _cache_reference_audio(reference_audio_bytes, reference_suffix or ".wav")
        ok, produced_path, reason = _synthesize_with_worker(
            config,
            text=qwen_text,
            ref_audio_path=ref_tmp,
            output_path=out_wav,
            language="auto",
            ref_text=qwen_ref_text,
            x_vector_only_mode=bool(x_vector_only_mode),
        )
        if not ok:
            return False, None, reason
        return True, produced_path, "ok"
    except Exception as exc:
        return False, None, str(exc)
    finally:
        pass


def synthesize_voice_clone_to_file(
    text: str,
    output_path: str,
    ref_audio_path: str,
    *,
    language: str = "Chinese",
    ref_text: str = "",
    x_vector_only_mode: bool = True,
) -> Tuple[bool, Optional[str], str]:
    config = load_qwen3_tts_config()
    ok, reason = ensure_model_available(config)
    if not ok:
        return False, None, reason
    if not ref_audio_path or not os.path.isfile(ref_audio_path):
        return False, None, f"reference audio not found: {ref_audio_path}"

    qwen_text = _to_simplified_chinese_for_qwen(text)
    qwen_ref_text = _to_simplified_chinese_for_qwen(ref_text or "")

    output_path = os.path.abspath(output_path)
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    return _synthesize_with_worker(
        config,
        text=qwen_text,
        ref_audio_path=ref_audio_path,
        output_path=output_path,
        language=language or config.language,
        ref_text=qwen_ref_text,
        x_vector_only_mode=bool(x_vector_only_mode),
    )
