import os
import re
import shutil
import subprocess
import tempfile
import time
import hashlib
from typing import Optional

from fastapi import Request
from pdf2image import convert_from_path
from sqlalchemy.orm import Session

from backend.app.deps import decode_access_token
from backend.app.models import User

_OPENCC_S2T = None
_OPENCC_S2T_IMPORT_FAILED = False


def is_truthy_env(name: str, default: str = "false") -> bool:
    raw = os.getenv(name, default)
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def make_alignment_id(audio_bytes: bytes, text: str, backend: str) -> str:
    h = hashlib.sha1()
    h.update(audio_bytes or b"")
    h.update((text or "").encode("utf-8", errors="ignore"))
    h.update((backend or "").encode("utf-8", errors="ignore"))
    return h.hexdigest()[:24]


def to_traditional_chinese_for_display(text: str) -> str:
    global _OPENCC_S2T, _OPENCC_S2T_IMPORT_FAILED
    if not text:
        return text
    if _OPENCC_S2T_IMPORT_FAILED:
        return text
    if _OPENCC_S2T is None:
        try:
            from opencc import OpenCC  # type: ignore

            _OPENCC_S2T = OpenCC("s2t")
        except Exception:
            _OPENCC_S2T_IMPORT_FAILED = True
            return text
    try:
        return _OPENCC_S2T.convert(text)
    except Exception:
        return text


def clamp_preview_speed(speed: float) -> float:
    try:
        value = float(speed)
    except Exception:
        value = 1.0
    return max(0.5, min(2.0, value))


def split_user_script_to_pages(raw_text: str, page_count: int) -> list[str]:
    result = ["" for _ in range(max(0, int(page_count or 0)))]
    text = str(raw_text or "").strip()
    if not text or not result:
        return result

    marker_patterns = [
        # Strict LLM handoff format: #PAGE_001# ... #END_PAGE_001#
        r"(?:^|[\n\r])\s*#?\s*PAGE[_\-\s]*0*([0-9]+)\s*#?\s*(?:[\n\r]+|$)",
        # 中文: 第1頁: ...
        r"(?:^|[\n\r])\s*第\s*([0-9一二三四五六七八九十百零兩]+)\s*頁(?:\s*[:：\-、，.]|\s+)",
        # 英文: Page 1: ... / Slide 1: ...
        r"(?:^|[\n\r])\s*(?:page|slide)\s*([0-9]+)(?:\s*[:：\-、，.]|\s+)",
        # 簡寫: P1: ...
        r"(?:^|[\n\r])\s*p\s*([0-9]+)(?:\s*[:：\-、，.]|\s+)",
    ]
    marker_re = re.compile("|".join(f"(?:{p})" for p in marker_patterns), re.IGNORECASE)
    matches = list(marker_re.finditer(text))

    def _parse_page_num(token: str) -> int | None:
        token = str(token or "").strip()
        if not token:
            return None
        if token.isdigit():
            return int(token)
        mapping = {"零": 0, "一": 1, "二": 2, "兩": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
        if token == "十":
            return 10
        if "十" in token:
            left, _, right = token.partition("十")
            left_v = mapping.get(left, 1 if left == "" else None)
            right_v = mapping.get(right, 0 if right == "" else None)
            if left_v is None or right_v is None:
                return None
            return left_v * 10 + right_v
        total = 0
        for ch in token:
            if ch not in mapping:
                return None
            total = total * 10 + mapping[ch]
        return total if total > 0 else None

    if matches:
        for i, m in enumerate(matches):
            raw_num = next((g for g in m.groups() if g), "")
            page_num = _parse_page_num(raw_num)
            if not page_num:
                continue
            start = m.end()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            idx = page_num - 1
            if 0 <= idx < len(result):
                body = text[start:end]
                body = re.sub(r"(?im)^\s*#?\s*(?:END[_\-\s]*PAGE|ENDPAGE)[_\-\s]*0*\d+\s*#?\s*$", "", body)
                result[idx] = body.strip()
        return result

    chunks = [c.strip() for c in re.split(r"\n\s*\n+", text) if c.strip()]
    for i in range(min(len(result), len(chunks))):
        result[i] = chunks[i]
    if not any(result):
        result[0] = text
    return result


def apply_audio_speed(src_path: str, speed: float) -> str:
    speed = clamp_preview_speed(speed)
    if abs(speed - 1.0) < 1e-3:
        return src_path

    suffix = os.path.splitext(src_path or "")[-1] or ".wav"
    out_fd, out_path = tempfile.mkstemp(prefix="slideai_speed_", suffix=suffix)
    os.close(out_fd)
    proc = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-i",
            src_path,
            "-filter:a",
            f"atempo={speed:.4f}",
            "-vn",
            out_path,
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if proc.returncode != 0 or not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
        msg = (proc.stderr or proc.stdout or "ffmpeg speed adjust failed").strip()
        raise RuntimeError(f"音檔調速失敗: {msg[:400]}")
    return out_path


def pregenerate_thumbnails_safe(pdf_path: str, thumb_dir: str, logger) -> None:
    """Best-effort thumbnail generation in background; never raises."""
    try:
        os.makedirs(thumb_dir, exist_ok=True)
        all_pages = convert_from_path(
            pdf_path,
            thread_count=2,
            poppler_path=os.getenv("POPPLER_PATH", None),
        )
        for page_num, img in enumerate(all_pages, start=1):
            final_path = os.path.join(thumb_dir, f"page_{page_num}.png")
            tmp_path = f"{final_path}.tmp"
            img.save(tmp_path, format="PNG")
            os.replace(tmp_path, final_path)
        logger.info(f"[UPLOAD][BG] Saved {len(all_pages)} thumbnails to {thumb_dir}")
    except Exception as thumb_err:
        logger.warning(f"[UPLOAD][BG] Thumbnail pre-generation failed: {thumb_err}")


def is_mock_mode() -> bool:
    return str(os.getenv("VIDEO_ABSTRACT_MOCK_MODE", "")).strip().lower() in {"1", "true", "yes", "on"}


def is_local_only_mode() -> bool:
    return str(os.getenv("VIDEO_ABSTRACT_LOCAL_ONLY", "")).strip().lower() in {"1", "true", "yes", "on"}


def try_get_current_user_from_request(request: Request, db: Session) -> Optional[User]:
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        return None
    token = auth.split(" ", 1)[1].strip()
    if not token:
        return None
    try:
        payload = decode_access_token(token)
        email = payload.get("sub")
        if not email:
            return None
        return db.query(User).filter_by(email=email).first()
    except Exception:
        return None
