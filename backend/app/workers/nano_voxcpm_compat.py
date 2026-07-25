"""Synchronous VoxCPM-compatible adapter backed by Nano-vLLM."""
from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import torch  # noqa: F401 - preload CUDA libraries before Nano-vLLM/flash-attn
from nanovllm_voxcpm import VoxCPM


class NanoVoxCPMCompat:
    def __init__(
        self,
        model_path: str | Path,
        *,
        devices: list[int] | None = None,
        inference_timesteps: int = 10,
        gpu_memory_utilization: float = 0.80,
    ):
        self.server = VoxCPM.from_pretrained(
            model=str(model_path),
            devices=devices or [0],
            inference_timesteps=inference_timesteps,
            max_num_seqs=1,
            gpu_memory_utilization=gpu_memory_utilization,
        )
        self.tts_model = SimpleNamespace(sample_rate=48000)
        self._prompts: dict[tuple[str, str], str] = {}
        self._references: dict[str, bytes] = {}

    def _prompt_id(self, wav_path: str, prompt_text: str) -> str:
        wav_bytes = Path(wav_path).read_bytes()
        key = (hashlib.sha256(wav_bytes).hexdigest(), prompt_text)
        if key not in self._prompts:
            path = Path(wav_path)
            self._prompts[key] = self.server.add_prompt(
                wav_bytes,
                path.suffix.removeprefix(".") or "wav",
                prompt_text,
            )
        return self._prompts[key]

    def _reference_latents(self, wav_path: str) -> bytes:
        wav_bytes = Path(wav_path).read_bytes()
        key = hashlib.sha256(wav_bytes).hexdigest()
        if key not in self._references:
            path = Path(wav_path)
            self._references[key] = self.server.encode_latents(
                wav_bytes,
                path.suffix.removeprefix(".") or "wav",
            )
        return self._references[key]

    def generate(
        self,
        text: str,
        *,
        prompt_wav_path: str | None = None,
        prompt_text: str = "",
        reference_wav_path: str | None = None,
        cfg_value: float = 2.0,
        inference_timesteps: int | None = None,
        seed: int | None = None,
        use_reference_conditioning: bool = False,
        **_unused,
    ) -> np.ndarray:
        if not prompt_wav_path:
            raise ValueError("Nano-vLLM compatibility mode requires prompt_wav_path")
        if not prompt_text:
            raise ValueError("Nano-vLLM compatibility mode requires prompt_text")
        options = {
            "target_text": text,
            "prompt_id": self._prompt_id(prompt_wav_path, prompt_text),
            "cfg_value": cfg_value,
            "seed": seed,
        }
        if use_reference_conditioning:
            options["ref_audio_latents"] = self._reference_latents(
                reference_wav_path or prompt_wav_path
            )
        chunks = list(self.server.generate(**options))
        return np.concatenate(chunks) if chunks else np.empty(0, dtype=np.float32)

    def stop(self) -> None:
        self.server.stop()
