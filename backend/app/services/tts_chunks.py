from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Iterable

import numpy as np
import soundfile as sf


def combine_tts_chunks(
    chunk_sources: Iterable[tuple[str, str | Path]],
    output_path: str | Path,
    *,
    silence_ms: float = 120.0,
) -> list[dict]:
    """Combine persisted TTS chunks and emit the standard chunk sidecar."""
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    chunks_dir = output.with_suffix(".chunks")
    shutil.rmtree(chunks_dir, ignore_errors=True)
    chunks_dir.mkdir(parents=True, exist_ok=True)

    audio_parts = []
    timeline = []
    sample_rate = None
    channel_shape = None
    cursor = 0.0
    sources = list(chunk_sources)
    for index, (text, source) in enumerate(sources):
        wav, rate = sf.read(str(source), dtype="float32", always_2d=False)
        if sample_rate is None:
            sample_rate = rate
            channel_shape = wav.shape[1:] if wav.ndim > 1 else ()
        if rate != sample_rate or (wav.shape[1:] if wav.ndim > 1 else ()) != channel_shape:
            raise ValueError(f"chunk {index + 1} sample format does not match")
        filename = f"chunk_{index + 1:03d}.wav"
        sf.write(str(chunks_dir / filename), wav, sample_rate)
        duration = float(len(wav)) / float(sample_rate or 1)
        timeline.append({
            "index": index,
            "text": text,
            "start": round(cursor, 6),
            "end": round(cursor + duration, 6),
            "duration": round(duration, 6),
            "filename": filename,
        })
        audio_parts.append(wav)
        cursor += duration
        if index < len(sources) - 1 and silence_ms > 0:
            silence_shape = (int(sample_rate * silence_ms / 1000.0),) + channel_shape
            audio_parts.append(np.zeros(silence_shape, dtype=np.float32))
            cursor += silence_ms / 1000.0

    if not audio_parts or sample_rate is None:
        raise ValueError("no TTS chunks supplied")
    sf.write(str(output), np.concatenate(audio_parts, axis=0), sample_rate)
    (chunks_dir / "chunks.json").write_text(
        json.dumps({"silence_ms": silence_ms, "chunks": timeline}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return timeline
