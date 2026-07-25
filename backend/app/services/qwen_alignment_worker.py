#!/usr/bin/env python3
"""Lazy, persistent Qwen ASR / forced-alignment worker for SlideAI.

The parent service owns this process and terminates its process group after an
idle timeout.  Models are intentionally loaded only by the first request.
"""
from __future__ import annotations

import json
import os
import sys
import traceback
from typing import Any

PREFIX = "__QWEN_ALIGN_RESULT__"
_ALIGNER: Any = None
_ASR: Any = None


def _language(value: str) -> str:
    text = str(value or "").lower().strip()
    if text.startswith("zh") or "chinese" in text:
        return "Chinese"
    if text.startswith("en") or "english" in text:
        return "English"
    if text.startswith("ja") or "japanese" in text:
        return "Japanese"
    if text.startswith("ko") or "korean" in text:
        return "Korean"
    if text.startswith("fr") or "french" in text:
        return "French"
    if text.startswith("de") or "german" in text:
        return "German"
    if text.startswith("es") or "spanish" in text:
        return "Spanish"
    if text.startswith("it") or "italian" in text:
        return "Italian"
    if text.startswith("ru") or "russian" in text:
        return "Russian"
    if text.startswith("pt") or "portuguese" in text:
        return "Portuguese"
    return "Chinese"


def _has_cjk(text: str) -> bool:
    return any("\u3400" <= char <= "\u9fff" for char in str(text or ""))


def _convert(text: str, mode: str) -> str:
    if not text:
        return text
    try:
        from opencc import OpenCC
        return OpenCC(mode).convert(text)
    except Exception:
        return text


def _models() -> tuple[Any, Any, dict[str, Any], str, str]:
    import torch
    from qwen_asr import Qwen3ASRModel, Qwen3ForcedAligner

    device = os.getenv("QWEN3_ASR_DEVICE", "cuda:0" if torch.cuda.is_available() else "cpu")
    dtype_name = os.getenv("QWEN3_ASR_DTYPE", "bfloat16" if torch.cuda.is_available() else "float32").lower().strip()
    dtype = getattr(torch, dtype_name, torch.bfloat16 if torch.cuda.is_available() else torch.float32)
    kwargs: dict[str, Any] = {"device_map": device, "dtype": dtype}
    attn = os.getenv("QWEN3_ASR_ATTN_IMPL", "eager").strip()
    if attn:
        kwargs["attn_implementation"] = attn
    asr_path = os.getenv("QWEN3_ASR_MODEL_PATH", "").strip() or os.getenv("QWEN3_ASR_MODEL_ID", "Qwen/Qwen3-ASR-1.7B")
    align_path = os.getenv("QWEN3_ALIGNER_MODEL_PATH", "").strip() or os.getenv("QWEN3_ALIGNER_MODEL_ID", "Qwen/Qwen3-ForcedAligner-0.6B")
    return Qwen3ASRModel, Qwen3ForcedAligner, kwargs, asr_path, align_path


def _align(request: dict[str, Any]) -> dict[str, Any]:
    global _ALIGNER, _ASR
    mode = str(request.get("alignment_mode") or "scripted").strip().lower()
    text = str(request.get("text") or "")
    lang = _language(str(request.get("language") or ""))
    audio_path = str(request["audio_path"])
    Qwen3ASRModel, Qwen3ForcedAligner, kwargs, asr_path, align_path = _models()

    if mode == "auto_asr":
        if _ASR is None:
            _ASR = Qwen3ASRModel.from_pretrained(
                asr_path,
                forced_aligner=align_path,
                forced_aligner_kwargs=dict(kwargs),
                **kwargs,
            )
        result = _ASR.transcribe(audio=audio_path, language=None, return_time_stamps=True)[0]
        backend = "qwen3-asr+forced-aligner"
        source_text = str(getattr(result, "text", "") or "")
        items = list(getattr(result, "time_stamps", None) or [])
    else:
        if not text.strip():
            raise ValueError("scripted alignment requires non-empty text")
        if _ALIGNER is None:
            _ALIGNER = Qwen3ForcedAligner.from_pretrained(align_path, **kwargs)
        qwen_text = _convert(text, "t2s") if lang == "Chinese" else text
        items = list(_ALIGNER.align(audio=audio_path, text=qwen_text, language=lang)[0])
        backend = "qwen3-forced-aligner"
        source_text = qwen_text

    words = []
    for item in items:
        start = float(getattr(item, "start_time", 0.0) or 0.0)
        end = float(getattr(item, "end_time", start) or start)
        token = str(getattr(item, "text", "") or "").strip()
        if not token:
            continue
        words.append({"text": token, "start": start, "end": max(end, start + 1e-3)})
    if not words:
        raise RuntimeError("qwen3_alignment_returned_no_words")
    if lang == "Chinese" or _has_cjk(source_text):
        source_text = _convert(source_text, "s2t")
        for word in words:
            word["text"] = _convert(word["text"], "s2t")
    return {"backend": backend, "words": words, "asr_text": source_text}


def main() -> None:
    print(f"{PREFIX}{json.dumps({'ready': True})}", flush=True)
    for raw in sys.stdin:
        request: dict[str, Any] = {}
        try:
            request = json.loads(raw)
            result = _align(request)
            result.update({"id": request.get("id"), "ok": True})
        except Exception as exc:
            result = {"id": request.get("id", ""), "ok": False, "error": str(exc), "trace": traceback.format_exc(limit=2)}
        print(f"{PREFIX}{json.dumps(result, ensure_ascii=False)}", flush=True)


if __name__ == "__main__":
    main()
