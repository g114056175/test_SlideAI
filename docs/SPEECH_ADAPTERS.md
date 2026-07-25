# External speech provider adapters

SlideAI keeps the Web API stable while allowing TTS, ASR and forced alignment
to be replaced independently. Set a provider to `command`, then point its
command variable at a local executable or script:

```dotenv
SLIDEAI_TTS_PROVIDER=command
SLIDEAI_TTS_ADAPTER_COMMAND=python /opt/slideai-adapters/tts.py

SLIDEAI_ASR_PROVIDER=command
SLIDEAI_ASR_ADAPTER_COMMAND=python /opt/slideai-adapters/asr.py

SLIDEAI_ALIGNMENT_PROVIDER=command
SLIDEAI_ALIGNMENT_ADAPTER_COMMAND=python /opt/slideai-adapters/alignment.py
```

The command receives one JSON object on standard input and must print one JSON
object on standard output. Diagnostic output should go to standard error.

## TTS contract

Input fields: `operation=synthesize`, `text`, `reference_audio_path`,
`reference_text`, and `output_path`.

Success output:

```json
{"ok": true, "provider": "my-tts", "output_path": "/path/from/input.wav"}
```

The adapter must write a valid audio file to `output_path`. It may call a
commercial HTTP API or a locally installed model.

## Reference ASR contract

Input fields: `operation=transcribe` and `audio_path`.

Success output:

```json
{"ok": true, "provider": "my-asr", "text": "recognized transcript"}
```

## Alignment contract

Input fields include `operation=align`, `audio_path`, `text`, `language`,
`alignment_mode`, subtitle split settings, and pause split settings.

Success output:

```json
{
  "ok": true,
  "provider": "my-aligner",
  "segments": [{"start": 0.0, "end": 1.2, "text": "字幕"}],
  "srt": "1\n00:00:00,000 --> 00:00:01,200\n字幕\n",
  "audio_duration": 1.2
}
```

Only configure trusted commands. These adapters execute with the same local
permissions as the SlideAI backend.
