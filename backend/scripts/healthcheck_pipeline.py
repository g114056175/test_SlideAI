#!/usr/bin/env python3
"""
SlideAI pipeline health check.

Usage:
  python backend/scripts/healthcheck_pipeline.py
  python backend/scripts/healthcheck_pipeline.py --run-qwen-tts
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover
    load_dotenv = None


REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = REPO_ROOT / "backend"
ENV_FILE = BACKEND_ROOT / ".env"

# Ensure imports like `backend.app.services...` work when run as script.
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.app.services.qwen3_tts import (  # noqa: E402
    ensure_model_available,
    load_qwen3_tts_config,
    synthesize_voice_clone_to_file,
)


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str


def _check_cmd_exists(cmd: str) -> CheckResult:
    path = shutil.which(cmd)
    return CheckResult(
        name=f"command:{cmd}",
        ok=bool(path),
        detail=path or "not found on PATH",
    )


def _check_env_var(name: str, required: bool = True) -> CheckResult:
    val = os.getenv(name, "").strip()
    ok = bool(val) if required else True
    if not required and not val:
        return CheckResult(name=f"env:{name}", ok=True, detail="(optional) not set")
    detail = val if val else "missing"
    if val and any(x in name.upper() for x in ["KEY", "TOKEN", "SECRET", "PASSWORD"]):
        detail = f"{val[:3]}***{val[-3:]}" if len(val) > 6 else "***"
    return CheckResult(name=f"env:{name}", ok=ok, detail=detail)


def _check_path_exists(name: str, value: str, must_be_file: bool = False) -> CheckResult:
    p = Path(value)
    if must_be_file:
        ok = p.is_file()
    else:
        ok = p.exists()
    kind = "file" if must_be_file else "path"
    return CheckResult(name=f"{kind}:{name}", ok=ok, detail=str(p))


def _check_python_import(runtime_python: str, module_name: str) -> CheckResult:
    if not runtime_python or not Path(runtime_python).is_file():
        return CheckResult(
            name=f"import:{module_name}",
            ok=False,
            detail=f"runtime python not found: {runtime_python}",
        )
    proc = subprocess.run(
        [
            runtime_python,
            "-c",
            f"import importlib.util; raise SystemExit(0 if importlib.util.find_spec({module_name!r}) else 1)",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    return CheckResult(
        name=f"import:{module_name}",
        ok=proc.returncode == 0,
        detail=runtime_python if proc.returncode == 0 else (proc.stderr or proc.stdout or "missing"),
    )


def _default_ai_workspace() -> Path | None:
    bundled = REPO_ROOT.parent / "AI_Workspace"
    if bundled.is_dir():
        return bundled
    legacy_sibling = REPO_ROOT.parent.parent / "AI_Workspace"
    if legacy_sibling.is_dir():
        return legacy_sibling
    return None


def _print_results(results: List[CheckResult]) -> int:
    print("\n=== SlideAI Health Check ===")
    fail_count = 0
    for r in results:
        tag = "PASS" if r.ok else "FAIL"
        print(f"[{tag}] {r.name:<28} {r.detail}")
        if not r.ok:
            fail_count += 1
    print(f"\nSummary: {len(results) - fail_count} passed, {fail_count} failed")
    return fail_count


def main() -> int:
    parser = argparse.ArgumentParser(description="Check SlideAI pipeline and Qwen3 TTS path")
    parser.add_argument(
        "--run-qwen-tts",
        action="store_true",
        help="Run an actual Qwen3 TTS smoke test (needs QWEN3_TTS_REF_AUDIO_PATH).",
    )
    args = parser.parse_args()

    if load_dotenv is not None:
        load_dotenv(dotenv_path=ENV_FILE)
    elif ENV_FILE.is_file():
        # Minimal .env loader fallback to avoid hard dependency on python-dotenv.
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            raw = line.strip()
            if not raw or raw.startswith("#") or "=" not in raw:
                continue
            k, v = raw.split("=", 1)
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if k and k not in os.environ:
                os.environ[k] = v

    results: List[CheckResult] = []

    # Core toolchain checks for regular flow.
    results.append(_check_cmd_exists("ffmpeg"))
    results.append(_check_cmd_exists("ffprobe"))
    results.append(_check_cmd_exists("pdftoppm"))

    local_only = os.getenv("VIDEO_ABSTRACT_LOCAL_ONLY", "").strip().lower() in {"1", "true", "yes", "on"}

    # Basic env checks. LLM key is optional for the handoff demo when local-only mode is enabled.
    results.append(_check_env_var("api_key", required=not local_only))
    results.append(_check_env_var("THREAD_COUNT", required=False))
    results.append(CheckResult("mode:local_only", True, str(local_only)))

    # Qwen3 config checks.
    cfg = load_qwen3_tts_config()
    results.append(CheckResult("qwen3:enabled", cfg.enabled, f"enabled={cfg.enabled}"))
    results.append(_check_path_exists("QWEN3_TTS_RUNTIME_PYTHON", cfg.runtime_python, must_be_file=True))
    results.append(CheckResult("qwen3:model_path", Path(cfg.model_path).is_dir(), cfg.model_path))
    results.append(CheckResult("qwen3:model_id", bool(cfg.model_id), cfg.model_id))
    results.append(CheckResult("qwen3:auto_download", True, str(cfg.auto_download)))

    model_ok, model_reason = ensure_model_available(cfg)
    results.append(CheckResult("qwen3:ensure_model_available", model_ok, model_reason))

    # Qwen3 ASR / forced aligner checks used by subtitle alignment.
    ai_workspace = _default_ai_workspace()
    asr_runtime = (
        os.getenv("QWEN3_ASR_RUNTIME_PYTHON", "").strip()
        or os.getenv("QWEN3_TTS_RUNTIME_PYTHON", "").strip()
        or (str(ai_workspace / "QWEN3-TTS/.venv/bin/python3") if ai_workspace else "")
    )
    asr_model_path = (
        os.getenv("QWEN3_ASR_MODEL_PATH", "").strip()
        or (str(ai_workspace / "QWEN3-ASR/Qwen3-ASR-1.7B") if ai_workspace else "")
    )
    aligner_model_path = (
        os.getenv("QWEN3_ALIGNER_MODEL_PATH", "").strip()
        or (str(ai_workspace / "QWEN3-ASR/Qwen3-ForcedAligner-0.6B") if ai_workspace else "")
    )
    results.append(_check_path_exists("QWEN3_ASR_RUNTIME_PYTHON", asr_runtime, must_be_file=True))
    if asr_model_path:
        results.append(_check_path_exists("QWEN3_ASR_MODEL_PATH", asr_model_path))
    else:
        results.append(CheckResult("path:QWEN3_ASR_MODEL_PATH", False, "missing"))
    if aligner_model_path:
        results.append(_check_path_exists("QWEN3_ALIGNER_MODEL_PATH", aligner_model_path))
    else:
        results.append(CheckResult("path:QWEN3_ALIGNER_MODEL_PATH", False, "missing"))
    results.append(_check_python_import(asr_runtime, "qwen_asr"))
    results.append(_check_python_import(cfg.runtime_python, "qwen_tts"))

    # Optional functional smoke test for "must route through Qwen".
    if args.run_qwen_tts:
        ref_path = os.getenv("QWEN3_TTS_REF_AUDIO_PATH", "").strip()
        if not ref_path:
            results.append(
                CheckResult(
                    "qwen3:smoke_ref_audio",
                    False,
                    "QWEN3_TTS_REF_AUDIO_PATH is missing",
                )
            )
        else:
            results.append(_check_path_exists("QWEN3_TTS_REF_AUDIO_PATH", ref_path, must_be_file=True))
            if Path(ref_path).is_file():
                tmp_out = Path(tempfile.gettempdir()) / "slideai_qwen_healthcheck.wav"
                ok, out_path, reason = synthesize_voice_clone_to_file(
                    text="這是 SlideAI Qwen3 TTS 健康檢查。",
                    output_path=str(tmp_out),
                    ref_audio_path=ref_path,
                    language=os.getenv("QWEN3_TTS_LANGUAGE", "Chinese"),
                    ref_text=os.getenv("QWEN3_TTS_REF_TEXT", ""),
                    x_vector_only_mode=os.getenv("QWEN3_TTS_X_VECTOR_ONLY_MODE", "true").strip().lower()
                    in {"1", "true", "yes", "on"},
                )
                results.append(
                    CheckResult(
                        "qwen3:smoke_synthesis",
                        ok and bool(out_path),
                        reason if not ok else f"ok -> {out_path}",
                    )
                )
                if ok and out_path and Path(out_path).exists():
                    try:
                        Path(out_path).unlink(missing_ok=True)
                    except Exception:
                        pass
    else:
        results.append(
            CheckResult(
                "qwen3:smoke_synthesis",
                True,
                "skipped (use --run-qwen-tts to verify real synthesis path)",
            )
        )

    fail_count = _print_results(results)
    return 1 if fail_count else 0


if __name__ == "__main__":
    raise SystemExit(main())
