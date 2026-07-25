#!/usr/bin/env python3
"""Line-delimited JSON worker that keeps Nano-vLLM VoxCPM2 warm for SlideAI."""
from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path

import soundfile as sf

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from nano_voxcpm_compat import NanoVoxCPMCompat

PREFIX = "__NANO_VOX_RESULT__"


def main() -> None:
    adapter = NanoVoxCPMCompat(
        sys.argv[1],
        inference_timesteps=int(sys.argv[2]),
        gpu_memory_utilization=float(sys.argv[3]),
    )
    print(f"{PREFIX}{json.dumps({'ready': True})}", flush=True)
    try:
        for line in sys.stdin:
            request: dict = {}
            try:
                request = json.loads(line)
                wav = adapter.generate(
                    request["text"],
                    prompt_wav_path=request["reference_audio_path"],
                    prompt_text=request["prompt_text"],
                    reference_wav_path=request["reference_audio_path"],
                    cfg_value=float(request.get("cfg_value", 2.0)),
                    inference_timesteps=int(request.get("inference_timesteps", 10)),
                )
                if wav is None or len(wav) == 0:
                    raise RuntimeError("Nano-vLLM returned an empty waveform; no WAV was written")
                output = request["output_path"]
                sf.write(output, wav, adapter.tts_model.sample_rate)
                result = {"id": request["id"], "ok": True, "output_path": output}
            except Exception as exc:
                result = {
                    "id": request.get("id", ""),
                    "ok": False,
                    "error": str(exc),
                    "trace": traceback.format_exc(limit=2),
                }
            print(f"{PREFIX}{json.dumps(result, ensure_ascii=False)}", flush=True)
    finally:
        adapter.stop()


if __name__ == "__main__":
    main()
