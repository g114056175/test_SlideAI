# SlideAI handoff runbook

Create a source-only copy:

```bash
./scripts/create_handoff_bundle.sh /srv/slideai-handoff
```

The default excludes model weights, virtual environments, Node dependencies,
generated videos and the local database. To copy the four known checkpoint
directories into the standardized slots, add `--with-models`; model Python
environments are still excluded.

On the destination:

```bash
cd /srv/slideai-handoff/SlideAI
./slideai.sh build
./slideai.sh start
```

Required model layout:

```text
models/
├── tts/VoxCPM2/
├── tts/Qwen3-TTS-12Hz-1.7B-Base/       # optional quality fallback
├── asr/Qwen3-ASR-1.7B/
└── alignment/Qwen3-ForcedAligner-0.6B/
```

The portable default uses official VoxCPM2, Qwen3-ASR and Qwen3 ForcedAligner.
Nano-vLLM is an explicit optional acceleration profile selected during setup.
All three providers can be replaced independently without changing frontend
routes; see `docs/SPEECH_ADAPTERS.md`.

Deployment and Docker GPU requirements are documented in `deploy/README.md`.
