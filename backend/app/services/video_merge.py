"""FFmpeg helpers for lossless concatenation and optional page transitions."""

from __future__ import annotations

import asyncio
import json
import os
import random
import secrets
from pathlib import Path


# Keep the automatic set restrained: no black/white flashes, cube rotations, or
# aggressive zooms.  Directional effects make page changes visible without
# overwhelming the presentation content.
TRANSITION_EFFECTS = (
    "fade",
    "dissolve",
    "smoothleft",
    "smoothright",
    "coverleft",
    "coverright",
    "revealleft",
    "revealright",
)
DEFAULT_TRANSITION_SECONDS = 0.42


async def _run(command: list[str]) -> tuple[int, bytes, bytes]:
    proc = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    return proc.returncode or 0, stdout or b"", stderr or b""


async def _probe_media(path: str) -> dict:
    code, stdout, stderr = await _run([
        "ffprobe", "-v", "error", "-show_streams", "-show_format",
        "-of", "json", path,
    ])
    if code != 0:
        raise RuntimeError(f"無法讀取影片資訊：{stderr.decode(errors='ignore')[-300:]}")
    try:
        payload = json.loads(stdout.decode("utf-8"))
    except Exception as exc:
        raise RuntimeError("ffprobe 回傳格式錯誤") from exc
    streams = payload.get("streams") or []
    video = next((stream for stream in streams if stream.get("codec_type") == "video"), None)
    if not video:
        raise RuntimeError(f"影片沒有視訊軌：{path}")
    try:
        duration = float((payload.get("format") or {}).get("duration") or video.get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0.0
    if duration <= 0:
        raise RuntimeError(f"影片長度無效：{path}")
    return {
        "duration": duration,
        "width": int(video.get("width") or 1920),
        "height": int(video.get("height") or 1080),
        "has_audio": any(stream.get("codec_type") == "audio" for stream in streams),
    }


def _even(value: int) -> int:
    value = max(2, int(value))
    return value if value % 2 == 0 else value - 1


async def _concat_video_files(input_paths: list[str], temp_dir: str) -> tuple[str, dict]:
    list_file = os.path.join(temp_dir, "concat.txt")
    with open(list_file, "w", encoding="utf-8") as handle:
        for path in input_paths:
            safe_path = str(path).replace("'", "'\\''")
            handle.write(f"file '{safe_path}'\n")

    out_path = os.path.join(temp_dir, "merged.mp4")
    code, _stdout, stderr = await _run([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_file,
        "-c", "copy", "-movflags", "+faststart", out_path,
    ])
    if code != 0 or not os.path.isfile(out_path) or os.path.getsize(out_path) == 0:
        out_path = os.path.join(temp_dir, "merged_reencode.mp4")
        code2, _stdout2, stderr2 = await _run([
            "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_file,
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "19",
            "-c:a", "aac", "-b:a", "192k", "-pix_fmt", "yuv420p",
            "-movflags", "+faststart", out_path,
        ])
        if code2 != 0 or not os.path.isfile(out_path) or os.path.getsize(out_path) == 0:
            message = (stderr + b"\n" + stderr2).decode(errors="ignore").strip()
            raise RuntimeError(f"影片合併失敗：{message[-500:]}")
    return out_path, {"enabled": False, "seed": None, "effects": [], "duration_seconds": 0.0}


async def _merge_with_transitions(
    input_paths: list[str],
    temp_dir: str,
    transition_seed: int | None,
) -> tuple[str, dict]:
    media = [await _probe_media(path) for path in input_paths]
    target_width = _even(media[0]["width"])
    target_height = _even(media[0]["height"])
    minimum_duration = min(item["duration"] for item in media)
    transition_seconds = min(DEFAULT_TRANSITION_SECONDS, max(0.18, minimum_duration * 0.2))

    seed = int(transition_seed) if transition_seed is not None else secrets.randbits(63)
    rng = random.Random(seed)
    effects: list[str] = []
    previous = ""
    for _ in range(len(input_paths) - 1):
        choices = [effect for effect in TRANSITION_EFFECTS if effect != previous]
        effect = rng.choice(choices)
        effects.append(effect)
        previous = effect

    command = ["ffmpeg", "-y"]
    for path in input_paths:
        command.extend(["-i", path])

    filters: list[str] = []
    for index, item in enumerate(media):
        video_chain = (
            f"[{index}:v]settb=AVTB,setpts=PTS-STARTPTS,fps=30,"
            f"scale={target_width}:{target_height}:force_original_aspect_ratio=decrease,"
            f"pad={target_width}:{target_height}:(ow-iw)/2:(oh-ih)/2,setsar=1,format=yuv420p"
        )
        if index < len(media) - 1:
            video_chain += f",tpad=stop_mode=clone:stop_duration={transition_seconds:.6f}"
        filters.append(f"{video_chain}[v{index}]")

        if item["has_audio"]:
            filters.append(
                f"[{index}:a]aformat=sample_rates=48000:channel_layouts=stereo,"
                f"aresample=async=1:first_pts=0,asetpts=PTS-STARTPTS[a{index}]"
            )
        else:
            filters.append(
                f"anullsrc=r=48000:cl=stereo,atrim=duration={item['duration']:.6f},"
                f"asetpts=PTS-STARTPTS[a{index}]"
            )

    previous_label = "v0"
    elapsed = media[0]["duration"]
    for index, effect in enumerate(effects, start=1):
        output_label = f"vx{index}"
        filters.append(
            f"[{previous_label}][v{index}]xfade=transition={effect}:"
            f"duration={transition_seconds:.6f}:offset={elapsed:.6f}[{output_label}]"
        )
        previous_label = output_label
        elapsed += media[index]["duration"]

    audio_inputs = "".join(f"[a{index}]" for index in range(len(media)))
    filters.append(f"{audio_inputs}concat=n={len(media)}:v=0:a=1[aout]")

    out_path = os.path.join(temp_dir, "merged_transitions.mp4")
    command.extend([
        "-filter_complex", ";".join(filters),
        "-map", f"[{previous_label}]", "-map", "[aout]",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "19",
        "-c:a", "aac", "-b:a", "192k", "-pix_fmt", "yuv420p",
        "-movflags", "+faststart", "-shortest", out_path,
    ])
    code, _stdout, stderr = await _run(command)
    if code != 0 or not os.path.isfile(out_path) or os.path.getsize(out_path) == 0:
        raise RuntimeError(f"轉場影片合併失敗：{stderr.decode(errors='ignore')[-700:]}")

    return out_path, {
        "enabled": True,
        "seed": seed,
        "effects": effects,
        "duration_seconds": round(transition_seconds, 3),
        "output_size": [target_width, target_height],
    }


async def merge_video_files(
    input_paths: list[str],
    temp_dir: str,
    *,
    transitions_enabled: bool = False,
    transition_seed: int | None = None,
) -> tuple[str, dict]:
    """Merge videos and return ``(output_path, transition_metadata)``."""
    paths = [str(Path(path).resolve()) for path in input_paths]
    if not paths:
        raise ValueError("沒有可合併的片段")
    for path in paths:
        if not os.path.isfile(path) or os.path.getsize(path) == 0:
            raise ValueError(f"找不到有效影片片段：{path}")
    os.makedirs(temp_dir, exist_ok=True)
    if transitions_enabled and len(paths) > 1:
        return await _merge_with_transitions(paths, temp_dir, transition_seed)
    return await _concat_video_files(paths, temp_dir)
