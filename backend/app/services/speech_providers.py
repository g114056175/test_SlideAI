"""Stable speech-provider facade for SlideAI.

Built-in providers preserve the current VoxCPM/Qwen workflow.  A deployment
can replace TTS, reference ASR, or subtitle alignment independently by setting
the corresponding provider to ``command`` and implementing the JSON contract
documented in ``docs/SPEECH_ADAPTERS.md``.
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from backend.app.services.qwen3_tts import (
    synthesize_voice_clone_preview,
    warm_qwen3_tts_worker_async,
)
from backend.app.services.subtitle_alignment import (
    AlignmentResult,
    align_subtitles_from_audio_and_text,
)
from backend.app.services.voxtts import (
    synthesize_voxtts_preview,
    transcribe_with_local_qwen3_asr,
)


def get_tts_provider_name() -> str:
    configured = os.getenv("SLIDEAI_TTS_PROVIDER", "").strip().lower()
    if configured:
        return configured
    legacy = os.getenv("SLIDEAI_TTS_ENGINE", "voxcpm_nano").strip().lower()
    return "qwen3" if legacy == "qwen3" else "voxcpm_nano"


def get_asr_provider_name() -> str:
    return os.getenv("SLIDEAI_ASR_PROVIDER", "qwen3").strip().lower()


def get_alignment_provider_name() -> str:
    return os.getenv("SLIDEAI_ALIGNMENT_PROVIDER", "qwen3").strip().lower()


def tts_requires_reference_text() -> bool:
    return get_tts_provider_name() in {"voxcpm", "voxcpm_nano", "nano_vllm"}


def _run_command_adapter(env_name: str, payload: dict[str, Any], timeout: int) -> dict[str, Any]:
    command = os.getenv(env_name, "").strip()
    if not command:
        raise RuntimeError(f"{env_name} is required when provider=command")
    proc = subprocess.run(
        shlex.split(command),
        input=json.dumps(payload, ensure_ascii=False) + "\n",
        capture_output=True,
        text=True,
        timeout=timeout,
        env=os.environ.copy(),
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "adapter failed").strip()
        raise RuntimeError(f"{env_name} failed: {detail[:1200]}")
    for line in reversed((proc.stdout or "").splitlines()):
        try:
            result = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(result, dict):
            if not result.get("ok", True):
                raise RuntimeError(str(result.get("error") or "adapter returned ok=false"))
            return result
    raise RuntimeError(f"{env_name} returned no JSON object")


def synthesize_tts_preview(
    *,
    text: str,
    reference_audio_bytes: bytes,
    reference_suffix: str = ".wav",
    reference_text: str = "",
) -> tuple[bool, str | None, str]:
    provider = get_tts_provider_name()
    if provider in {"voxcpm", "voxcpm_nano", "nano_vllm"}:
        return synthesize_voxtts_preview(
            text=text,
            reference_audio_bytes=reference_audio_bytes,
            reference_suffix=reference_suffix,
            auxiliary_text=reference_text,
            use_prompt_text=True,
        )
    if provider == "qwen3":
        return synthesize_voice_clone_preview(
            text=text,
            reference_audio_bytes=reference_audio_bytes,
            reference_suffix=reference_suffix,
            ref_text=reference_text,
            x_vector_only_mode=None,
        )
    if provider != "command":
        return False, None, f"unsupported TTS provider: {provider}"

    ref_path = ""
    out_fd, out_path = tempfile.mkstemp(prefix="slideai_tts_adapter_", suffix=".wav")
    os.close(out_fd)
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=reference_suffix or ".wav") as fp:
            fp.write(reference_audio_bytes)
            ref_path = fp.name
        result = _run_command_adapter(
            "SLIDEAI_TTS_ADAPTER_COMMAND",
            {
                "operation": "synthesize",
                "text": text,
                "reference_audio_path": ref_path,
                "reference_text": reference_text,
                "output_path": out_path,
            },
            int(os.getenv("SLIDEAI_TTS_ADAPTER_TIMEOUT_SEC", "600")),
        )
        produced = str(result.get("output_path") or out_path)
        if not Path(produced).is_file():
            return False, None, "TTS adapter did not create output_path"
        return True, produced, f"command:{result.get('provider', 'custom')}"
    except Exception as exc:
        return False, None, str(exc)
    finally:
        if ref_path:
            Path(ref_path).unlink(missing_ok=True)


def warm_tts_provider() -> dict[str, Any]:
    provider = get_tts_provider_name()
    if provider in {"voxcpm", "voxcpm_nano", "nano_vllm", "command"}:
        return {"ok": True, "engine": provider, "deferred": True}
    if provider == "qwen3":
        warm_qwen3_tts_worker_async()
        return {"ok": True, "engine": "qwen3"}
    return {"ok": False, "engine": provider, "error": "unsupported provider"}


def transcribe_reference_audio(
    reference_audio_bytes: bytes,
    reference_suffix: str = ".wav",
) -> tuple[bool, str, str]:
    provider = get_asr_provider_name()
    if provider == "qwen3":
        return transcribe_with_local_qwen3_asr(reference_audio_bytes, reference_suffix)
    if provider != "command":
        return False, "", f"unsupported ASR provider: {provider}"

    audio_path = ""
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=reference_suffix or ".wav") as fp:
            fp.write(reference_audio_bytes)
            audio_path = fp.name
        result = _run_command_adapter(
            "SLIDEAI_ASR_ADAPTER_COMMAND",
            {"operation": "transcribe", "audio_path": audio_path},
            int(os.getenv("SLIDEAI_ASR_ADAPTER_TIMEOUT_SEC", "600")),
        )
        return True, str(result.get("text") or ""), f"command:{result.get('provider', 'custom')}"
    except Exception as exc:
        return False, "", str(exc)
    finally:
        if audio_path:
            Path(audio_path).unlink(missing_ok=True)


def align_subtitles(
    *,
    text: str,
    audio_bytes: bytes | None = None,
    audio_source_path: str | None = None,
    audio_filename: str,
    language: str = "auto",
    alignment_mode: str = "auto",
    split_min_chars: int = 10,
    split_max_chars: int = 32,
    enable_pause_split: bool = False,
    pause_threshold_ms: int = 320,
) -> AlignmentResult:
    provider = get_alignment_provider_name()
    if provider == "qwen3":
        return align_subtitles_from_audio_and_text(
            text=text,
            audio_bytes=audio_bytes,
            audio_source_path=audio_source_path,
            audio_filename=audio_filename,
            language=language,
            alignment_mode=alignment_mode,
            split_min_chars=split_min_chars,
            split_max_chars=split_max_chars,
            enable_pause_split=enable_pause_split,
            pause_threshold_ms=pause_threshold_ms,
        )
    if provider != "command":
        raise RuntimeError(f"unsupported alignment provider: {provider}")

    audio_path = ""
    owns_audio_path = False
    try:
        if audio_source_path:
            audio_path = str(Path(audio_source_path).expanduser().resolve())
            if not Path(audio_path).is_file():
                raise FileNotFoundError(f"audio file not found: {audio_path}")
        else:
            suffix = Path(audio_filename or "audio.wav").suffix or ".wav"
            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as fp:
                fp.write(audio_bytes or b"")
                audio_path = fp.name
            owns_audio_path = True
        result = _run_command_adapter(
            "SLIDEAI_ALIGNMENT_ADAPTER_COMMAND",
            {
                "operation": "align",
                "audio_path": audio_path,
                "text": text,
                "language": language,
                "alignment_mode": alignment_mode,
                "split_min_chars": split_min_chars,
                "split_max_chars": split_max_chars,
                "enable_pause_split": enable_pause_split,
                "pause_threshold_ms": pause_threshold_ms,
            },
            int(os.getenv("SLIDEAI_ALIGNMENT_ADAPTER_TIMEOUT_SEC", "900")),
        )
        return AlignmentResult(
            segments=list(result.get("segments") or []),
            srt=str(result.get("srt") or ""),
            backend=f"command:{result.get('provider', 'custom')}",
            audio_duration=float(result.get("audio_duration") or 0.0),
            warning=str(result.get("warning") or ""),
            readable_chunks=list(result.get("readable_chunks") or []),
            source_text=str(result.get("source_text") or text),
            match_ratio=float(result.get("match_ratio") or 1.0),
        )
    finally:
        if audio_path and owns_audio_path:
            Path(audio_path).unlink(missing_ok=True)
