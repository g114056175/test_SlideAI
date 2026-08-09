# Model storage

Large model files are intentionally excluded from Git. Upload or download
checkpoints into the following stable slots:

```text
models/
├── tts/
│   ├── VoxCPM2/
│   └── Qwen3-TTS-12Hz-1.7B-Base/   # optional fallback
├── asr/
│   └── Qwen3-ASR-1.7B/
└── alignment/
    └── Qwen3-ForcedAligner-0.6B/
```

These directories are only convenient defaults. Existing checkpoints do not
need to be copied: set each `*_MODEL_HOST_PATH` in `deploy/models.env`, and
Docker mounts every directory read-only into its expected container slot.
Model Python environments are built into the backend Docker image.

Download sources and local paths are configured in
`deploy/models.env.example`. TTS, ASR and alignment can also be replaced by
command adapters; see `docs/SPEECH_ADAPTERS.md`.
