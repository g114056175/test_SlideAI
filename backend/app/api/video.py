import os
import asyncio
import subprocess
import tempfile
import shutil
import base64
import zipfile
import json
import ast
import uuid
import logging
import threading
import time
import hashlib
import re
from fastapi import APIRouter, UploadFile, File, Request, Query, HTTPException, Response, Depends
from fastapi.responses import FileResponse, JSONResponse
import io
from pdf2image import convert_from_path
from pydantic import BaseModel
from PIL import Image, UnidentifiedImageError
import urllib.parse
import json
from sqlalchemy.orm import Session
from backend.app.models import User
from backend.app.deps import (
    get_db, get_current_user, check_daily_usage_limit, create_file_record,
    record_usage, create_project_record, update_project_video_url, delete_project,
    decode_access_token,
    MAX_FILE_SIZE, FILE_RETENTION_DAYS, DAILY_USAGE_LIMIT,
)
from backend.app.models import Project
from typing import Optional, List
import PyPDF2
from backend.app.services.speech_providers import (
    align_subtitles,
    get_tts_provider_name,
    synthesize_tts_preview,
    transcribe_reference_audio,
    tts_requires_reference_text,
    warm_tts_provider,
)
from backend.app.services.artifact_store import get_video_run_store
logger = logging.getLogger("video_abstract")
logging.basicConfig(level=logging.INFO)

router = APIRouter()
_OPENCC_S2T = None
_OPENCC_S2T_IMPORT_FAILED = False
_ALIGNMENT_CACHE: dict[str, dict] = {}
_ALIGNMENT_CACHE_TTL_SEC = 900 # 15 minutes
MAX_CACHE_ENTRIES = 3


def _is_truthy_env(name: str, default: str = "false") -> bool:
    raw = os.getenv(name, default)
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _purge_alignment_cache() -> None:
    now = time.time()
    expired = [k for k, v in _ALIGNMENT_CACHE.items() if now - float(v.get("ts", 0)) > _ALIGNMENT_CACHE_TTL_SEC]
    for k in expired:
        _ALIGNMENT_CACHE.pop(k, None)

    if len(_ALIGNMENT_CACHE) > MAX_CACHE_ENTRIES:
        sorted_keys = sorted(_ALIGNMENT_CACHE.keys(), key=lambda k: float(_ALIGNMENT_CACHE[k].get("ts", 0)))
        for k in sorted_keys[:-MAX_CACHE_ENTRIES]:
            _ALIGNMENT_CACHE.pop(k, None)


def _make_alignment_id(audio_bytes: bytes, text: str, backend: str) -> str:
    h = hashlib.sha1()
    h.update(audio_bytes or b"")
    h.update((text or "").encode("utf-8", errors="ignore"))
    h.update((backend or "").encode("utf-8", errors="ignore"))
    return h.hexdigest()[:24]


def _to_traditional_chinese_for_display(text: str) -> str:
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


def _clamp_preview_speed(speed: float) -> float:
    try:
        value = float(speed)
    except Exception:
        value = 1.0
    return max(0.5, min(2.0, value))


def _split_user_script_to_pages(raw_text: str, page_count: int) -> list[str]:
    result = ["" for _ in range(max(0, int(page_count or 0)))]
    text = str(raw_text or "").strip()
    if not text or not result:
        return result

    # Primary format used by the script editor / LLM:
    # #PAGE_001#
    # ...
    # #END_PAGE_001#
    # The old parser only handled "第1頁：" markers, which caused tagged
    # user input to collapse into fallback chunks instead of page-specific text.
    tagged: dict[int, str] = {}
    for i in range(len(result)):
        page_no = i + 1
        tag_re = re.compile(
            rf"(?:^|\n)\s*#?\s*PAGE[_\-\s]*0*{page_no}\s*#?\s*\n?"
            rf"(.*?)"
            rf"(?=(?:^|\n)\s*#?\s*(?:END[_\-\s]*PAGE|ENDPAGE)[_\-\s]*0*{page_no}\s*#?|"
            rf"(?:^|\n)\s*#?\s*PAGE[_\-\s]*0*{page_no + 1}\s*#?|\Z)",
            flags=re.IGNORECASE | re.DOTALL,
        )
        match = tag_re.search(text)
        if match:
            body = re.sub(
                r"(?im)^\s*#?\s*(?:PAGE|END[_\-\s]*PAGE|ENDPAGE)[_\-\s]*\d+\s*#?\s*$",
                "",
                match.group(1),
            ).strip()
            if body:
                tagged[i] = body
    if tagged:
        for idx, body in tagged.items():
            if 0 <= idx < len(result):
                result[idx] = body
        return result

    # 僅在「行首/句首」視為分頁標記，避免把「第三頁會再說明」誤判為切段。
    marker_re = re.compile(
        r"(?:^|[\n\r])\s*第\s*([0-9一二三四五六七八九十百零兩]+)\s*頁(?:\s*[:：\-、，.]|\s+)",
        re.IGNORECASE,
    )
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
            page_num = _parse_page_num(m.group(1))
            if not page_num:
                continue
            start = m.end()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            idx = page_num - 1
            if 0 <= idx < len(result):
                result[idx] = text[start:end].strip()
        return result

    # fallback: 依雙換行切
    chunks = [c.strip() for c in re.split(r"\n\s*\n+", text) if c.strip()]
    for i in range(min(len(result), len(chunks))):
        result[i] = chunks[i]
    if not any(result):
        result[0] = text
    return result


def _apply_audio_speed(src_path: str, speed: float) -> str:
    speed = _clamp_preview_speed(speed)
    if abs(speed - 1.0) < 1e-3:
        return src_path

    suffix = os.path.splitext(src_path or "")[-1] or ".wav"
    out_path = tempfile.mktemp(suffix=suffix)
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


def _pregenerate_thumbnails_safe(pdf_path: str, thumb_dir: str) -> None:
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


def _pregenerate_run_thumbnails_safe(run_id: str, pdf_path: str) -> None:
    """Best-effort page image cache for the persistent run store.

    This runs in a daemon thread after upload so the UI can enter the workspace
    immediately while previews become available progressively.
    """
    try:
        store = get_video_run_store()
        images = convert_from_path(
            pdf_path,
            thread_count=2,
            poppler_path=os.getenv("POPPLER_PATH", None),
        )
        for page_idx, img in enumerate(images):
            pdir = store.page_dir(run_id, page_idx)
            pdir.mkdir(parents=True, exist_ok=True)
            img = img.convert("RGB")
            max_w = 1280
            if img.width > max_w:
                ratio = max_w / max(img.width, 1)
                img = img.resize((max_w, max(1, int(img.height * ratio))), Image.Resampling.LANCZOS)
            out_path = pdir / f"page_{page_idx + 1:03d}.jpg"
            tmp_path = pdir / f"{out_path.name}.tmp"
            img.save(tmp_path, format="JPEG", quality=82, optimize=True)
            os.replace(tmp_path, out_path)
            try:
                store.record_page_asset(run_id=run_id, page_index=page_idx)
            except Exception:
                pass
        logger.info(f"[UPLOAD][BG] Cached {len(images)} run thumbnails for run={run_id}")
    except Exception as thumb_err:
        logger.warning(f"[UPLOAD][BG] Run thumbnail cache failed run={run_id}: {thumb_err}")


class TextsRequest(BaseModel):
    texts: list[str]
    pdf_id: str
    resolution: int = 1080
    tts_model: str = 'voxcpm_nano'
    voice: str = 'zh-TW-YunJheNeural'
    enable_subtitles: bool = True
    # 可擴充更多影片選項


def _is_mock_mode() -> bool:
    return str(os.getenv("VIDEO_ABSTRACT_MOCK_MODE", "")).strip().lower() in {"1", "true", "yes", "on"}

def _is_local_only_mode() -> bool:
    return str(os.getenv("VIDEO_ABSTRACT_LOCAL_ONLY", "")).strip().lower() in {"1", "true", "yes", "on"}


def _use_nano_voxcpm_tts() -> bool:
    return get_tts_provider_name() in {"voxcpm_nano", "nano_vllm", "voxcpm"}


def _try_get_current_user_from_request(request: Request, db: Session) -> Optional[User]:
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

@router.post("/api/video-abstract")
async def video_abstract_api(
    request: Request,
    file: UploadFile = File(None),
    db: Session = Depends(get_db),
):
    """
    1. 若有 file，回傳 AI 文字陣列（JSON，不產生影片）
    2. 若為 application/json 且有 texts，產生影片並回傳影片檔案
    """

    mock_mode = _is_mock_mode()
    local_only_mode = _is_local_only_mode()
    current_user = _try_get_current_user_from_request(request, db)

    if not mock_mode and not local_only_mode and current_user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")

    if file:
        # If a video file is uploaded, handle as video-abstract video upload
        if file.content_type and file.content_type.startswith("video/"):
            # usage limit
            if not check_daily_usage_limit(current_user, db):
                raise HTTPException(status_code=429, detail=f"今日使用次數已達上限({DAILY_USAGE_LIMIT}次)，請明天再試")

            # check file size
            file.file.seek(0, os.SEEK_END)
            size = file.file.tell()
            file.file.seek(0)
            if size > MAX_FILE_SIZE:
                raise HTTPException(status_code=400, detail=f"檔案過大，請上傳 {MAX_FILE_SIZE // 1024 // 1024}MB 以下的影片")

            # save video file
            files_dir = os.path.join(os.getcwd(), "user_files")
            os.makedirs(files_dir, exist_ok=True)
            file_extension = os.path.splitext(file.filename)[1]
            unique_filename = f"{uuid.uuid4()}{file_extension}"
            file_path = os.path.join(files_dir, unique_filename)
            with open(file_path, "wb") as buffer:
                shutil.copyfileobj(file.file, buffer)

            # create file record
            file_record = create_file_record(
                user=current_user,
                file_name=file.filename,
                file_path=file_path,
                file_type="video_abstract",
                file_size=size,
                db=db
            )

            try:
                # placeholder AI analysis
                result = f"這是影片 {file.filename} 的 AI 摘要。影片內容分析完成，包含關鍵場景、重要對話和主要情節。"
                file_record.analysis_result = result
                file_record.status = 'completed'
                db.commit()
                record_usage(current_user, "video_abstract", db)
                return JSONResponse({
                    "result": result,
                    "file_id": file_record.id,
                    "expires_at": file_record.expires_at.isoformat(),
                    "retention_days": FILE_RETENTION_DAYS
                })
            except Exception as e:
                if os.path.exists(file_path):
                    os.remove(file_path)
                db.delete(file_record)
                db.commit()
                raise HTTPException(status_code=500, detail=f'處理失敗: {str(e)}')

        # 產生唯一 pdf_id 並儲存 PDF
        pdf_id = str(uuid.uuid4())
        pdf_dir = os.path.join(os.path.dirname(__file__), "..", "tmp_pdf")
        pdf_dir = os.path.abspath(pdf_dir)
        os.makedirs(pdf_dir, exist_ok=True)
        pdf_path = os.path.join(pdf_dir, f"{pdf_id}.pdf")
        logger.info(f"[PDF UPLOAD] Saving PDF to {pdf_path}")
        with open(pdf_path, "wb") as f:
            shutil.copyfileobj(file.file, f)
        logger.info(f"[PDF UPLOAD] PDF saved: {os.path.exists(pdf_path)} size={os.path.getsize(pdf_path) if os.path.exists(pdf_path) else 0}")

        # 僅在系統啟動時檢查 Poppler，這裡不下載/檢查。
        # 只產生 AI 文字，不產生影片
        local_only_mode = _is_local_only_mode()
        try:
            form = await request.form()
            content_language = form.get('content_language') if 'content_language' in form else None
            voice_hint = form.get('voice') if 'voice' in form else None
            language_hint = form.get('language') if 'language' in form else None
            skip_llm_raw = str(form.get('skip_llm', '')).strip().lower()
            skip_llm = skip_llm_raw in {"1", "true", "yes", "on"}
            subtitle_source = str(form.get('subtitle_source', '')).strip()
            user_script = str(form.get('user_script', '') or '')
        except Exception:
            content_language = None
            voice_hint = None
            language_hint = None
            skip_llm = False
            subtitle_source = ""
            user_script = ""

        with open(pdf_path, "rb") as pdf_file:
            reader = PyPDF2.PdfReader(pdf_file)
            page_count = len(reader.pages)

        if subtitle_source == "none":
            ai_texts = ["" for _ in range(page_count)]
            logger.info(f"[UPLOAD] subtitle_source=none, return {len(ai_texts)} empty scripts")
        elif subtitle_source == "user_input":
            ai_texts = _split_user_script_to_pages(user_script, page_count)
            logger.info(f"[UPLOAD] subtitle_source=user_input, parsed {len(ai_texts)} page scripts")
        elif mock_mode or local_only_mode or skip_llm:
            # Fast path for lab/demo: when skipping LLM, only count pages.
            ai_texts = ["" for _ in range(page_count)]
            logger.info(
                f"[UPLOAD] Skip LLM (mock={mock_mode}, local_only={local_only_mode}, "
                f"skip_llm={skip_llm}, subtitle_source={subtitle_source!r}), return empty scripts for {len(ai_texts)} pages"
            )
        else:
            from backend.app.services.utility.pdf import pdf_to_text_array
            text_array = pdf_to_text_array(pdf_path)
            from backend.app.services.utility.api import generate_presentation_scripts
            script = "請根據每一頁內容生成簡報稿，語氣簡潔明確。"
            from dotenv import load_dotenv
            dotenv_path = os.path.join(os.path.dirname(__file__), '..', '.env')
            load_dotenv(dotenv_path=dotenv_path)
            api_key = os.getenv("api_key")
            if not api_key:
                raise RuntimeError(".env 未設置 api_key")
            # 優先使用 content_language，其次 language，再用 voice 提示
            detected_language = content_language or language_hint or voice_hint
            logger.info(f"[UPLOAD] Detected language for AI generation: {detected_language}")
            ai_texts = await generate_presentation_scripts(
                text_array=text_array,
                script=script,
                api_key=api_key,
                language=detected_language,
            )

        # Optional persistent thumbnails (default OFF for cache-light mode).
        if not _is_truthy_env("VIDEO_ABSTRACT_DISABLE_PERSISTENT_THUMBNAILS", "true"):
            thumb_base = os.path.join(os.path.dirname(__file__), "..", "user_thumbnails")
            thumb_base = os.path.abspath(thumb_base)
            thumb_dir = os.path.join(thumb_base, pdf_id)
            threading.Thread(
                target=_pregenerate_thumbnails_safe,
                args=(pdf_path, thumb_dir),
                daemon=True,
            ).start()

        # Create a project record so the job is tracked from the start.
        project_name = file.filename or os.path.basename(pdf_path)
        project_id = None
        if current_user is not None:
            project = create_project_record(
                user=current_user,
                project_name=project_name,
                pdf_path=pdf_path,
                script_json=None,
                db=db,
                pdf_id=pdf_id,
            )
            project_id = project.id
            logger.info(f"[UPLOAD] Created project record id={project.id} name={project_name}")
        elif mock_mode:
            logger.info("[UPLOAD][MOCK] No auth user, skip project record creation")

        run_manifest = None
        run_id = None
        try:
            run_manifest = get_video_run_store().create_run(
                pdf_path=pdf_path,
                original_filename=project_name,
                scripts=ai_texts,
                settings={
                    "upload": {
                        "subtitle_source": subtitle_source or ("none" if skip_llm else ""),
                        "content_language": content_language,
                        "voice": voice_hint,
                        "language": language_hint,
                        "skip_llm": skip_llm,
                    }
                },
                pdf_id=pdf_id,
                project_id=project_id,
                source="web-upload",
            )
            run_id = str(run_manifest.get("run_id") or "")
            logger.info(f"[UPLOAD] Created video run id={run_id} name={project_name}")
            threading.Thread(
                target=_pregenerate_run_thumbnails_safe,
                args=(run_id, pdf_path),
                daemon=True,
            ).start()
        except Exception as run_err:
            # Upload should still succeed even if the optional persistent run
            # store has a transient filesystem issue, but log loudly because the
            # Lab workflow expects run_id for resumable artifacts.
            logger.error(f"[UPLOAD] Failed to create persistent video run: {run_err}", exc_info=True)

        return JSONResponse({
            "texts": ai_texts,
            "pdf_id": pdf_id,
            "project_id": project_id,
            "run_id": run_id,
            "run": run_manifest,
            # The lightweight app-only launcher intentionally has no LLM/model
            # services. Tell the SPA not to immediately start a second,
            # model-dependent request after the PDF upload has succeeded.
            "model_services_skipped": bool(mock_mode),
        })

    # 舊版 JSON 產片主流程已停用（EdgeTTS / 舊字幕估算 / 舊 MoviePy 管線）
    if request.headers.get("content-type", "").startswith("application/json"):
        raise HTTPException(
            status_code=410,
            detail="舊版 JSON 影片生成流程已停用。請使用 /video-abstract-lab 的 QwenTTS + Qwen 強對齊 + ASS 渲染流程。",
        )
    return JSONResponse({"detail": "請上傳 PDF 或傳送 texts"}, status_code=400)


@router.get("/api/video-abstract/thumbnail")
async def video_abstract_thumbnail(pdf_id: str = Query(...), page: int = Query(1)):
    """Return a PNG thumbnail for a given PDF page.

    Resolution order:
    1. user_thumbnails/<pdf_id>/page_N.png  (persistent, generated at upload)
    2. tmp_pdf/<pdf_id>.pdf rendered on-demand via pdf2image (fallback for
       in-progress uploads before the persistent PNGs are written).

    Query params:
    - pdf_id: the UUID returned when the PDF was uploaded
    - page: 1-based page index
    """
    # 1. Check persistent thumbnail store first (optional).
    if not _is_truthy_env("VIDEO_ABSTRACT_DISABLE_PERSISTENT_THUMBNAILS", "true"):
        thumb_base = os.path.join(os.path.dirname(__file__), "..", "user_thumbnails")
        thumb_base = os.path.abspath(thumb_base)
        persistent_png = os.path.join(thumb_base, pdf_id, f"page_{page}.png")
        if os.path.exists(persistent_png):
            try:
                # Validate PNG integrity to avoid serving half-written/corrupted files.
                with Image.open(persistent_png) as im:
                    im.verify()
                logger.info(f"[THUMBNAIL] Serving persistent thumbnail: {persistent_png}")
                with open(persistent_png, "rb") as f:
                    return Response(content=f.read(), media_type="image/png")
            except (UnidentifiedImageError, OSError, SyntaxError) as img_err:
                logger.warning(f"[THUMBNAIL] Persistent thumbnail invalid, fallback to render: {img_err}")

    # 2. Fall back to rendering from the temp PDF
    pdf_dir = os.path.join(os.path.dirname(__file__), "..", "tmp_pdf")
    pdf_dir = os.path.abspath(pdf_dir)
    pdf_path = os.path.join(pdf_dir, f"{pdf_id}.pdf")
    logger.info(f"[THUMBNAIL] Persistent PNG not found; trying tmp_pdf: {pdf_path} page={page}")
    if not os.path.exists(pdf_path):
        logger.warning(f"[THUMBNAIL] PDF not found in tmp_pdf either: {pdf_path}")
        raise HTTPException(status_code=404, detail="Thumbnail not found")

    try:
        images = convert_from_path(
            pdf_path,
            first_page=page,
            last_page=page,
            thread_count=1,
            poppler_path=os.getenv("POPPLER_PATH", None)
        )
        if not images:
            raise HTTPException(status_code=500, detail="Failed to render PDF page")
        img = images[0]
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)
        return Response(content=buf.getvalue(), media_type="image/png")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[THUMBNAIL] Error rendering thumbnail: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Error rendering thumbnail")


# ---------------------------------------------------------------------------
# Project history endpoints
# ---------------------------------------------------------------------------

@router.get("/api/projects")
async def list_projects(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Return all non-deleted projects for the authenticated user, newest first.
    """
    projects = (
        db.query(Project)
        .filter(
            Project.user_id == current_user.id,
            Project.status != 'deleted',
        )
        .order_by(Project.created_at.desc())
        .all()
    )
    return JSONResponse([
        {
            "id":           p.id,
            "project_name": p.project_name,
            "pdf_id":       p.pdf_id,
            "pdf_path":     p.pdf_path,
            "video_url":    p.video_url,
            "script_json":  p.script_json,
            "status":       p.status,
            "created_at":   p.created_at.isoformat() if p.created_at else None,
        }
        for p in projects
    ])


@router.delete("/api/projects/{project_id}")
async def delete_project_endpoint(
    project_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Soft-delete a project.  The video file is removed from disk; the database
    row is kept with status='deleted' for audit purposes.
    """
    result = delete_project(
        project_id=project_id,
        user_id=current_user.id,
        db=db,
    )

    if not result['found']:
        raise HTTPException(status_code=404, detail="Project not found.")
    if result['forbidden']:
        raise HTTPException(status_code=403, detail="Not authorized to delete this project.")

    logger.info(
        f"[DELETE PROJECT] project_id={project_id} user_id={current_user.id} "
        f"file_deleted={result['file_deleted']}"
    )
    return JSONResponse({"detail": "Project deleted.", "file_deleted": result['file_deleted']})


# ── TTS 試聽 Preview Endpoint ──────────────────────────────────────────────
from fastapi import Form
import tempfile, asyncio

@router.post("/api/video-abstract/tts-preview")
async def tts_preview_endpoint(
    text: str = Form(...),
    voice: str = Form("zh-TW-YunJheNeural"),
    speed: float = Form(1.0),
    reference_audio: Optional[UploadFile] = File(None),
    reference_text: str = Form(""),
):
    """
    TTS 試聽生成端點，透過部署時選定的 provider 進行語音克隆。
    """
    import os

    speed = _clamp_preview_speed(speed)
    out_tmp = tempfile.mktemp(suffix=".mp3")

    local_only_mode = _is_local_only_mode()

    ref_data = None
    file_suffix = ".wav"
    if reference_audio is not None:
        ref_data = await reference_audio.read()
        file_suffix = os.path.splitext(reference_audio.filename or "")[-1] or ".wav"

    if ref_data:
        try:
            provider = get_tts_provider_name()
            if tts_requires_reference_text() and not (reference_text or "").strip():
                raise HTTPException(
                    status_code=400,
                    detail=f"{provider} 語音克隆需要參考音檔逐字稿；請先使用「ASR 代填」或填入 reference text。",
                )
            ok, out_wav, reason = await asyncio.to_thread(
                synthesize_tts_preview,
                text=text,
                reference_audio_bytes=ref_data,
                reference_suffix=file_suffix,
                reference_text=reference_text or "",
            )
            if ok and out_wav and os.path.exists(out_wav):
                final_out = await asyncio.to_thread(_apply_audio_speed, out_wav, speed)
                return FileResponse(final_out, media_type="audio/wav", filename="tts_preview.wav")
            if local_only_mode:
                logger.error(f"[TTS Preview] LOCAL_ONLY mode and {provider} unavailable: {reason}")
                raise HTTPException(status_code=500, detail=f"LOCAL_ONLY 模式下 {provider} 失敗：{reason}")
            raise HTTPException(status_code=500, detail=f"{provider} 失敗：{reason}")
        except Exception as e:
            if isinstance(e, HTTPException):
                raise
            if local_only_mode:
                logger.error(f"[TTS Preview] LOCAL_ONLY mode and TTS exception: {e}")
                raise HTTPException(status_code=500, detail=f"LOCAL_ONLY 模式下 TTS 例外：{str(e)}")
            raise HTTPException(status_code=500, detail=f"TTS 例外：{str(e)}")
    elif local_only_mode:
        raise HTTPException(status_code=400, detail="LOCAL_ONLY 模式下請提供參考音檔，才能使用本地 TTS 生成。")
    raise HTTPException(status_code=400, detail="請提供參考音檔以生成語音。EdgeTTS fallback 已停用。")


@router.post("/api/video-abstract/tts-warmup")
async def tts_warmup_endpoint():
    """
    Warm up the configured TTS provider when appropriate.
    The worker still shuts down automatically after the configured idle timeout.
    """
    return JSONResponse(await asyncio.to_thread(warm_tts_provider))


@router.post("/api/video-runs/{run_id}/pages/{page_index}/tts")
async def video_run_page_tts_endpoint(
    run_id: str,
    page_index: int,
    text: str = Form(...),
    voice: str = Form("zh-TW-YunJheNeural"),
    speed: float = Form(1.0),
    reference_audio: Optional[UploadFile] = File(None),
    reference_text: str = Form(""),
):
    """Generate one page TTS, persist it immediately, and return the audio."""
    import os
    if page_index < 0:
        raise HTTPException(status_code=400, detail="page_index must be >= 0")
    ref_data = await reference_audio.read() if reference_audio is not None else None
    if not ref_data:
        raise HTTPException(status_code=400, detail="請提供參考音檔以生成語音。")
    file_suffix = os.path.splitext(reference_audio.filename or "")[-1] or ".wav"
    if tts_requires_reference_text() and not (reference_text or "").strip():
        raise HTTPException(
            status_code=400,
            detail=f"{get_tts_provider_name()} 語音克隆需要參考音檔逐字稿。",
        )
    ok, out_wav, reason = await asyncio.to_thread(
        synthesize_tts_preview,
        text=text,
        reference_audio_bytes=ref_data,
        reference_suffix=file_suffix,
        reference_text=reference_text or "",
    )
    if not ok or not out_wav or not os.path.exists(out_wav):
        raise HTTPException(status_code=500, detail=f"TTS 失敗：{reason}")
    final_out = await asyncio.to_thread(_apply_audio_speed, out_wav, speed)
    audio_bytes = await asyncio.to_thread(lambda: open(final_out, "rb").read())
    variant = get_video_run_store().record_page_variant_tts(
        run_id=run_id,
        page_index=page_index,
        audio_bytes=audio_bytes,
        metadata={
            "text": text,
            "voice": voice,
            "speed": speed,
            "reference_text": reference_text or "",
        },
        label=f"web-page-{page_index + 1}",
    )
    return FileResponse(
        variant["paths"]["audio"],
        media_type="audio/wav",
        filename=f"page_{page_index + 1}_tts.wav",
        headers={
            "X-TTS-Id": variant["variant_id"],
            "X-Variant-Id": variant["variant_id"],
        },
    )


@router.post("/api/video-runs/{run_id}/pages/{page_index}/align")
async def video_run_page_align_endpoint(
    run_id: str,
    page_index: int,
    text: str = Form(""),
    language: str = Form("auto"),
    alignment_mode: str = Form("auto"),
    split_min_chars: int = Form(10),
    split_max_chars: int = Form(32),
    enable_pause_split: bool = Form(False),
    pause_threshold_ms: int = Form(320),
    tts_id: str = Form(""),
    variant_id: str = Form(""),
    audio_file: UploadFile = File(...),
):
    """Align one page audio, persist segments immediately, and return them."""
    if page_index < 0:
        raise HTTPException(status_code=400, detail="page_index must be >= 0")
    audio_bytes = await audio_file.read()
    result = await asyncio.to_thread(
        align_subtitles,
        text=text,
        audio_bytes=audio_bytes,
        audio_filename=audio_file.filename or "audio.wav",
        language=language,
        alignment_mode=alignment_mode,
        split_min_chars=split_min_chars,
        split_max_chars=split_max_chars,
        enable_pause_split=enable_pause_split,
        pause_threshold_ms=pause_threshold_ms,
    )
    target_variant_id = variant_id or tts_id
    if not target_variant_id:
        raise HTTPException(status_code=400, detail="variant_id is required for persistent alignment")
    variant = get_video_run_store().record_page_variant_alignment(
        run_id=run_id,
        page_index=page_index,
        variant_id=target_variant_id,
        segments=result.segments,
        srt=result.srt,
        metadata={
            "text": text,
            "backend": result.backend,
            "tts_id": tts_id,
            "split_min_chars": split_min_chars,
            "split_max_chars": split_max_chars,
            "readable_chunks": result.readable_chunks or [],
            "source_text_preview": (result.source_text or "")[:300],
        },
    )
    align_id = variant["variant_id"]
    return JSONResponse(
        {
            "align_id": align_id,
            "variant_id": variant["variant_id"],
            "segments": result.segments,
            "srt": result.srt,
            "backend": result.backend,
            "audio_duration": result.audio_duration,
            "readable_chunks": result.readable_chunks or [],
        },
        headers={"X-Align-Id": align_id, "X-Variant-Id": variant["variant_id"]},
    )


@router.post("/api/video-abstract/reference-asr")
async def reference_asr_fill_endpoint(
    reference_audio: UploadFile = File(...),
):
    try:
        ref_data = await reference_audio.read()
        file_suffix = os.path.splitext(reference_audio.filename or "")[-1] or ".wav"
        ok, text, reason = await asyncio.to_thread(
            transcribe_reference_audio,
            ref_data,
            file_suffix,
        )
        if not ok:
            raise HTTPException(status_code=500, detail=reason)
        display_text = _to_traditional_chinese_for_display(str(text or "").strip())
        return JSONResponse({"text": display_text})
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[Reference ASR] failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"本地 ASR 代填失敗: {str(e)}")


@router.post("/api/video-abstract/subtitle-align")
async def subtitle_align_endpoint(
    text: str = Form(""),
    language: str = Form("auto"),
    alignment_mode: str = Form("auto"),
    split_min_chars: int = Form(10),
    split_max_chars: int = Form(32),
    enable_pause_split: bool = Form(False),
    pause_threshold_ms: int = Form(320),
    audio_file: UploadFile = File(...),
):
    """
    臨時字幕對齊端點：接收音檔 + 可選文字，回傳對齊後 segments 與 SRT。
    自動策略：
      - 有文字：Qwen3-ForcedAligner（強制對齊）
      - 無文字：Qwen3-ASR + ForcedAligner
    """
    try:
        audio_bytes = await audio_file.read()
        result = await asyncio.to_thread(
            align_subtitles,
            text=text,
            audio_bytes=audio_bytes,
            audio_filename=audio_file.filename or "audio.wav",
            language=language,
            alignment_mode=alignment_mode,
            split_min_chars=split_min_chars,
            split_max_chars=split_max_chars,
            enable_pause_split=enable_pause_split,
            pause_threshold_ms=pause_threshold_ms,
        )
        _purge_alignment_cache()
        alignment_id = _make_alignment_id(audio_bytes, text, result.backend)
        _ALIGNMENT_CACHE[alignment_id] = {
            "ts": time.time(),
            "segments": result.segments,
            "backend": result.backend,
            "text": text,
            "readable_chunks": result.readable_chunks or [],
        }
        return JSONResponse(
            {
                "alignment_id": alignment_id,
                "segments": result.segments,
                "srt": result.srt,
                "backend": result.backend,
                "audio_duration": result.audio_duration,
                "readable_chunks": result.readable_chunks or [],
            }
        )
    except Exception as e:
        logger.error(f"[Subtitle Align] failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"字幕對齊失敗: {str(e)}")


@router.post("/api/video-abstract/render-subtitle-video")
async def render_subtitle_video_endpoint(
    audio_file: UploadFile = File(...),
    slide_image: UploadFile | None = File(None),
    segments_json: str = Form(""),
    alignment_id: str = Form(""),
    subtitle_style: str = Form("bg-dark"),
    enable_highlight: str = Form("true"),
    font_size: int = Form(20),
    bg_opacity: int = Form(68),
    align_backend: str = Form(""),
):
    """Deprecated endpoint. Use /api/video-abstract/render-subtitle-ass-video."""
    raise HTTPException(
        status_code=410,
        detail="render-subtitle-video 已停用。請改用 render-subtitle-ass-video。",
    )
    try:
        import json as _json

        request_start_ts = time.time()
        logger.info(f"[Render Subtitle Video] request received ts={request_start_ts}")

        audio_bytes = await audio_file.read()
        after_read_ts = time.time()
        logger.info(f"[Render Subtitle Video] files read ts={after_read_ts} elapsed={after_read_ts-request_start_ts:.3f}s")
        slide_bytes = await slide_image.read() if slide_image is not None else b""
        segments = []
        resolved_backend = str(align_backend or "")
        _purge_alignment_cache()
        if alignment_id and alignment_id in _ALIGNMENT_CACHE:
            cache_item = _ALIGNMENT_CACHE.get(alignment_id, {})
            segments = cache_item.get("segments") or []
            if not resolved_backend:
                resolved_backend = str(cache_item.get("backend") or "")
        elif segments_json:
            segments = _json.loads(segments_json)
        if not isinstance(segments, list) or len(segments) == 0:
            raise HTTPException(status_code=400, detail="缺少可用字幕時間軸（segments/alignment_id）")

        segments_resolved_ts = time.time()
        logger.info(f"[Render Subtitle Video] segments resolved ts={segments_resolved_ts} elapsed={segments_resolved_ts-request_start_ts:.3f}s segments={len(segments)} backend={resolved_backend}")

        # -- 寫入暫存檔 --
        tmp_dir = tempfile.mkdtemp()
        audio_path = os.path.join(tmp_dir, "audio" + (os.path.splitext(audio_file.filename or "audio.wav")[-1] or ".wav"))
        slide_path = os.path.join(tmp_dir, "slide_input.png")
        ass_path = os.path.join(tmp_dir, "subtitles.ass")
        output_path = os.path.join(tmp_dir, "subtitle_video.mp4")

        with open(audio_path, "wb") as f:
            f.write(audio_bytes)
        canvas_w, canvas_h = 1280, 720
        if slide_bytes:
            with open(slide_path, "wb") as f:
                f.write(slide_bytes)
            try:
                with Image.open(io.BytesIO(slide_bytes)) as im:
                    iw, ih = im.size
                if iw and ih:
                    canvas_w, canvas_h = int(iw), int(ih)
            except Exception:
                pass

        # -- 取得音頻時長 --
        probe = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", audio_path],
            capture_output=True, text=True, timeout=30
        )
        duration = 10.0
        try:
            probe_data = _json.loads(probe.stdout)
            duration = float(probe_data.get("format", {}).get("duration", 10.0))
        except Exception:
            pass

        # -- 優先使用 Canvas Renderer（shared layout + skia-canvas） --
        try:
            canvas_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "canvas_renderer"))
            canvas_render_js = os.path.join(canvas_dir, "render.mjs")
            canvas_output_path = os.path.join(tmp_dir, "subtitle_video_canvas.mp4")
            canvas_input_json = os.path.join(tmp_dir, "canvas_input.json")

            if os.path.exists(canvas_render_js) and slide_bytes and os.path.exists(slide_path):
                canvas_payload = {
                    "audioPath": audio_path,
                    "slidePath": slide_path,
                    "subtitleStyle": subtitle_style,
                    "fontSize": int(font_size),
                    "bgOpacity": int(bg_opacity),
                    "enableHighlight": str(enable_highlight).lower() in ("true", "1"),
                    "segments": segments,
                    "alignBackend": resolved_backend,
                    "fps": 30,
                    "width": canvas_w,
                    "height": canvas_h,
                }
                with open(canvas_input_json, "w", encoding="utf-8") as f:
                    _json.dump(canvas_payload, f, ensure_ascii=False)

                canvas_called_ts = time.time()
                logger.info(f"[Render Subtitle Video] calling canvas renderer ts={canvas_called_ts} elapsed={canvas_called_ts-request_start_ts:.3f}s")

                canvas_proc = subprocess.run(
                    ["node", canvas_render_js, canvas_input_json, canvas_output_path],
                    cwd=canvas_dir,
                    capture_output=True,
                    text=True,
                    timeout=900,
                )
                canvas_done_ts = time.time()
                logger.info(f"[Render Subtitle Video] canvas renderer returned code={canvas_proc.returncode} ts={canvas_done_ts} elapsed={canvas_done_ts-request_start_ts:.3f}s")

                if canvas_proc.returncode == 0 and os.path.exists(canvas_output_path):
                    with open(canvas_output_path, "rb") as f:
                        video_bytes = f.read()
                    resp_ts = time.time()
                    headers = {
                        "Content-Disposition": "attachment; filename=subtitle_video.mp4",
                        "X-Subtitle-Render-Version": "canvas-skia-v1",
                        "X-Subtitle-Style-Applied": subtitle_style,
                        "X-Server-Received-Ts": f"{request_start_ts}",
                        "X-Segments-Resolved-Ts": f"{segments_resolved_ts}",
                        "X-Canvas-Called-Ts": f"{canvas_called_ts}",
                        "X-Canvas-Done-Ts": f"{canvas_done_ts}",
                        "X-Server-Response-Ts": f"{resp_ts}",
                    }
                    logger.info(f"[Render Subtitle Video] responding ts={resp_ts} elapsed={resp_ts-request_start_ts:.3f}s headers={headers}")
                    return Response(
                        content=video_bytes,
                        media_type="video/mp4",
                        headers=headers,
                    )
                else:
                    logger.warning(
                        "[Render Subtitle Video] canvas renderer failed, fallback to remotion: %s",
                        (canvas_proc.stderr or canvas_proc.stdout or "unknown")[:500],
                    )
        except Exception as canvas_err:
            logger.warning(f"[Render Subtitle Video] canvas renderer exception, fallback to remotion: {canvas_err}")

        # -- 次優先使用 Remotion（與 WebUI CSS 行為較一致） --
        try:
            remotion_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "remotion_renderer"))
            remotion_render_js = os.path.join(remotion_dir, "render.mjs")
            remotion_output_path = os.path.join(tmp_dir, "subtitle_video_remotion.mp4")
            remotion_input_json = os.path.join(tmp_dir, "remotion_input.json")

            if os.path.exists(remotion_render_js) and slide_bytes and os.path.exists(slide_path):
                remotion_payload = {
                    "audioPath": audio_path,
                    "slidePath": slide_path,
                    "subtitleStyle": subtitle_style,
                    "fontSize": int(font_size),
                    "bgOpacity": int(bg_opacity),
                    "enableHighlight": str(enable_highlight).lower() in ("true", "1"),
                    "segments": segments,
                }
                with open(remotion_input_json, "w", encoding="utf-8") as f:
                    _json.dump(remotion_payload, f, ensure_ascii=False)

                remotion_proc = subprocess.run(
                    ["node", remotion_render_js, remotion_input_json, remotion_output_path],
                    cwd=remotion_dir,
                    capture_output=True,
                    text=True,
                    timeout=900,
                )
                if remotion_proc.returncode == 0 and os.path.exists(remotion_output_path):
                    with open(remotion_output_path, "rb") as f:
                        video_bytes = f.read()
                    return Response(
                        content=video_bytes,
                        media_type="video/mp4",
                        headers={
                            "Content-Disposition": "attachment; filename=subtitle_video.mp4",
                            "X-Subtitle-Render-Version": "remotion-v1",
                            "X-Subtitle-Style-Applied": subtitle_style,
                        },
                    )
                else:
                    logger.warning(
                        "[Render Subtitle Video] remotion failed, fallback to ass: %s",
                        (remotion_proc.stderr or remotion_proc.stdout or "unknown")[:500],
                    )
        except Exception as remotion_err:
            logger.warning(f"[Render Subtitle Video] remotion exception, fallback to ass: {remotion_err}")

        # 輸出解析度：僅字幕+音訊，採 1280x720 以降低生成成本
        W, H = 1280, 720
        do_highlight = str(enable_highlight).lower() in ("true", "1")
        scaled_font_size = max(11, int(font_size))

        def _to_ass_time(sec: float) -> str:
            sec = max(0.0, float(sec or 0.0))
            h = int(sec // 3600)
            m = int((sec % 3600) // 60)
            s = int(sec % 60)
            cs = int(round((sec - int(sec)) * 100))
            if cs >= 100:
                s += 1
                cs = 0
            return f"{h}:{m:02d}:{s:02d}.{cs:02d}"

        def _ass_escape(text: str) -> str:
            src = str(text or "")
            src = src.replace("\\", r"\\").replace("{", r"\{").replace("}", r"\}")
            src = src.replace("\r\n", "\n").replace("\r", "\n")
            return src.replace("\n", r"\N")

        def _ass_color_from_rgb(r: int, g: int, b: int) -> str:
            return f"&H{b:02X}{g:02X}{r:02X}&"

        def _ass_color_from_argb(a: int, r: int, g: int, b: int) -> str:
            aa = max(0, min(255, int(a)))
            return f"&H{aa:02X}{b:02X}{g:02X}{r:02X}&"

        def _style_for_mode(mode: str):
            opacity = max(0, min(100, int(bg_opacity)))
            alpha = int(round((100 - opacity) * 255 / 100))
            base = {
                "fontname": "Noto Sans CJK TC",
                "fontsize": scaled_font_size,
                "primary": _ass_color_from_rgb(255, 255, 255),
                # Secondary color is not used by our current highlight strategy.
                # Keep it same as primary to avoid renderer-specific karaoke surprises.
                "secondary": _ass_color_from_rgb(255, 255, 255),
                "outline": _ass_color_from_rgb(0, 0, 0),
                "back": _ass_color_from_argb(alpha, 0, 0, 0),
                "bold": 0,
                "border_style": 1,
                "outline_w": 0,
                "shadow": 0,
            }
            if mode == "stroke-dark":
                base.update({"bold": -1, "outline_w": 2.0, "shadow": 0})
            elif mode == "stroke-light":
                base.update({"bold": -1, "outline": _ass_color_from_rgb(156, 163, 175), "outline_w": 2.0, "shadow": 0})
            elif mode == "bg-gray":
                # Tight opaque box around subtitle glyphs (not full-width bar)
                base.update({
                    "border_style": 3,
                    "outline_w": 1.0,
                    "outline": _ass_color_from_argb(alpha, 128, 128, 128),
                    "back": _ass_color_from_argb(alpha, 128, 128, 128),
                })
            else:
                base.update({
                    "border_style": 3,
                    "outline_w": 1.0,
                    "outline": _ass_color_from_argb(alpha, 0, 0, 0),
                    "back": _ass_color_from_argb(alpha, 0, 0, 0),
                })
            return base

        def _build_seg_plain_text(seg: dict) -> str:
            text = str(seg.get("text", "") or "")
            if text.strip():
                return text
            words = seg.get("words") or []
            return "".join(str(w.get("text", "")) for w in words)

        def _is_punc_or_space(s: str) -> bool:
            if not s:
                return True
            puncts = set("，。！？；：「」『』（）、,.!?;:'\"()[]{} \n\t\r")
            return all(ch in puncts for ch in s)

        def _build_highlight_events(seg: dict):
            # Return [(start, end, ass_text)] where ass_text already escaped/override-tagged.
            # This mimics WebUI behavior: normal text white, only active word yellow.
            words = seg.get("words") or []
            if not words:
                start = float(seg.get("start", 0.0) or 0.0)
                end = float(seg.get("end", start + 0.2) or (start + 0.2))
                if end <= start:
                    end = start + 0.2
                return [(start, end, _ass_escape(_build_seg_plain_text(seg)))]

            full_text = _build_seg_plain_text(seg)
            seg_start = float(seg.get("start", 0.0) or 0.0)
            seg_end = float(seg.get("end", seg_start + 0.2) or (seg_start + 0.2))
            if seg_end <= seg_start:
                seg_end = seg_start + 0.2

            raw_tokens = []
            for w in words:
                text = str(w.get("text", ""))
                ws = float(w.get("start", seg_start) or seg_start)
                we = float(w.get("end", ws) or ws)
                if not text:
                    continue
                raw_tokens.append({"text": text, "start": ws, "end": we})

            if not raw_tokens:
                return [(seg_start, seg_end, _ass_escape(full_text))]

            # Build a monotonic non-overlapping timeline to avoid ASS multi-event overlap artifacts.
            n = max(1, len(raw_tokens))
            seg_dur = max(0.001, seg_end - seg_start)
            min_dur = max(0.02, min(0.09, (seg_dur / n) * 0.9))

            tokens = []
            last_end = seg_start
            for tk in raw_tokens:
                t = str(tk["text"])
                ws = max(seg_start, float(tk["start"]))
                we = min(seg_end, float(tk["end"]))
                if ws < last_end:
                    ws = last_end
                if we <= ws:
                    we = min(seg_end, ws + min_dur)
                if we <= ws:
                    continue
                tokens.append({"text": t, "start": ws, "end": we})
                last_end = we

            if not tokens:
                return [(seg_start, seg_end, _ass_escape(full_text))]

            # Map token text back to full subtitle text indices.
            mapped = []
            cursor = 0
            for tk in tokens:
                t = tk["text"]
                pos = full_text.find(t, cursor)
                if pos < 0:
                    pos = cursor
                s_idx = max(0, min(len(full_text), pos))
                e_idx = max(s_idx, min(len(full_text), s_idx + len(t)))
                cursor = e_idx
                mapped.append((tk["start"], tk["end"], t, s_idx, e_idx))

            white = "&HFFFFFF&"
            yellow = "&H24BFFB&"
            events = []
            for ws, we, t, s_idx, e_idx in mapped:
                if _is_punc_or_space(t):
                    events.append((ws, we, _ass_escape(full_text)))
                    continue
                left = _ass_escape(full_text[:s_idx])
                mid = _ass_escape(full_text[s_idx:e_idx])
                right = _ass_escape(full_text[e_idx:])
                ass_text = (
                    r"{\1c" + white + "}" +
                    left +
                    r"{\1c" + yellow + "}" +
                    mid +
                    r"{\1c" + white + "}" +
                    right
                )
                events.append((ws, we, ass_text))

            if not events:
                start = float(seg.get("start", 0.0) or 0.0)
                end = float(seg.get("end", start + 0.2) or (start + 0.2))
                if end <= start:
                    end = start + 0.2
                return [(start, end, _ass_escape(full_text))]
            return events

        style = _style_for_mode(subtitle_style)
        ass_lines = [
            "[Script Info]",
            "ScriptType: v4.00+",
            "PlayResX: 1280",
            "PlayResY: 720",
            "ScaledBorderAndShadow: yes",
            "",
            "[V4+ Styles]",
            "Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,"
            "Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,"
            "Alignment,MarginL,MarginR,MarginV,Encoding",
            (
                f"Style: Default,{style['fontname']},{style['fontsize']},{style['primary']},{style['secondary']},"
                f"{style['outline']},{style['back']},{style['bold']},0,0,0,100,100,0,0,{style['border_style']},"
                f"{style['outline_w']},{style['shadow']},2,80,80,54,1"
            ),
            "",
            "[Events]",
            "Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text",
        ]
        for seg in segments:
            if do_highlight:
                # Base text always visible throughout segment to prevent flicker/gap disappearance.
                seg_start = float(seg.get("start", 0.0) or 0.0)
                seg_end = float(seg.get("end", seg_start + 0.2) or (seg_start + 0.2))
                if seg_end <= seg_start:
                    seg_end = seg_start + 0.2
                base_text = _ass_escape(_build_seg_plain_text(seg))
                ass_lines.append(
                    f"Dialogue: 0,{_to_ass_time(seg_start)},{_to_ass_time(seg_end)},Default,,0,0,0,,{base_text}"
                )
                for ev_start, ev_end, ev_text in _build_highlight_events(seg):
                    ass_lines.append(
                        f"Dialogue: 1,{_to_ass_time(ev_start)},{_to_ass_time(ev_end)},Default,,0,0,0,,{ev_text}"
                    )
            else:
                start = float(seg.get("start", 0.0) or 0.0)
                end = float(seg.get("end", start + 0.2) or (start + 0.2))
                if end <= start:
                    end = start + 0.2
                text = _ass_escape(_build_seg_plain_text(seg))
                ass_lines.append(f"Dialogue: 0,{_to_ass_time(start)},{_to_ass_time(end)},Default,,0,0,0,,{text}")

        with open(ass_path, "w", encoding="utf-8") as f:
            f.write("\n".join(ass_lines))

        # -- FFmpeg 組合影片（背景圖或黑底 + ASS + 音訊） --
        if slide_bytes and os.path.exists(slide_path):
            vf = (
                f"scale={W}:{H}:force_original_aspect_ratio=decrease,"
                f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=black,"
                f"ass={ass_path}"
            )
            cmd = [
                "ffmpeg", "-y",
                "-loop", "1",
                "-i", slide_path,
                "-i", audio_path,
                "-vf", vf,
                "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                "-c:a", "aac", "-b:a", "128k",
                "-pix_fmt", "yuv420p",
                "-shortest",
                output_path,
            ]
        else:
            vf = f"ass={ass_path}"
            cmd = [
                "ffmpeg", "-y",
                "-f", "lavfi",
                "-i", f"color=c=black:s={W}x{H}:d={max(duration, 0.2):.3f}",
                "-i", audio_path,
                "-vf", vf,
                "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                "-c:a", "aac", "-b:a", "128k",
                "-pix_fmt", "yuv420p",
                "-shortest",
                output_path,
            ]
        result_proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if result_proc.returncode != 0 or not os.path.exists(output_path):
            err_msg = (result_proc.stderr or result_proc.stdout or "ffmpeg failed").strip()
            raise RuntimeError(f"FFmpeg 合成失敗: {err_msg[:500]}")

        with open(output_path, "rb") as f:
            video_bytes = f.read()

        return Response(
            content=video_bytes,
            media_type="video/mp4",
            headers={
                "Content-Disposition": "attachment; filename=subtitle_video.mp4",
                "X-Subtitle-Render-Version": "ass-discrete-v2",
                "X-Subtitle-Style-Applied": subtitle_style,
            },
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[Render Subtitle Video] failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"影片生成失敗: {str(e)}")
    finally:
        try:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception:
            pass

from backend.app.services.ass_renderer import generate_ass_script
@router.post("/api/video-abstract/render-subtitle-ass-video")
async def render_subtitle_ass_video(
    audio_file: UploadFile = File(...),
    slide_image: UploadFile = File(None),
    segments_json: str = Form("[]"),
    alignment_id: str = Form(""),
    subtitle_style: str = Form("bg-dark"),
    subtitle_mode: str = Form("burn"),
    enable_highlight: bool = Form(True),
    font_size: int = Form(54),
    enable_background: bool = Form(True),
    bg_color: str = Form("#000000"),
    bg_opacity: int = Form(68),
    margin_v: int = Form(96),
    align_backend: str = Form(""),
    run_id: str = Form(""),
    page_index: int = Form(-1),
    variant_label: str = Form(""),
    tts_id: str = Form(""),
    align_id: str = Form(""),
    variant_id: str = Form(""),
    tts_voice: str = Form(""),
    tts_speed: str = Form(""),
    selected_voice_key: str = Form(""),
    reference_text: str = Form(""),
):
    """(ASS 引擎極速渲染) Create a video with ASS subtitles from audio and an image."""
    temp_dir = None
    try:
        if not audio_file.filename:
            raise HTTPException(status_code=400, detail="請上傳音檔")
        audio_bytes = await audio_file.read()

        slide_bytes = None
        if slide_image and slide_image.filename:
            slide_bytes = await slide_image.read()

        segments_list = []
        if segments_json and segments_json != "[]":
            try:
                segments_list = json.loads(segments_json)
            except Exception:
                pass

        if not segments_list:
            if alignment_id and alignment_id in _ALIGNMENT_CACHE:
                cache_item = _ALIGNMENT_CACHE.get(alignment_id, {})
                segments_list = cache_item.get("segments", [])

        subtitle_mode = str(subtitle_mode or "burn").strip().lower()
        if subtitle_mode not in {"none", "sidecar", "burn"}:
            raise HTTPException(status_code=400, detail="無效的字幕輸出模式")
        if subtitle_mode == "burn" and not segments_list:
            raise HTTPException(status_code=400, detail="缺少可用字幕時間軸")

        temp_dir = tempfile.mkdtemp(prefix="slideai_ass_temp_")
        audio_path = os.path.join(temp_dir, "audio" + os.path.splitext(audio_file.filename)[1])
        with open(audio_path, "wb") as f:
            f.write(audio_bytes)

        canvas_w, canvas_h = 1920, 1080
        slide_path = os.path.join(temp_dir, "slide.png")
        if slide_bytes:
            with open(slide_path, "wb") as f:
                f.write(slide_bytes)
        else:
            from PIL import Image
            img = Image.new('RGB', (canvas_w, canvas_h), color=(0, 255, 0))
            img.save(slide_path)

        ass_content = None
        ass_path = ""
        if subtitle_mode == "burn":
            is_qwen = "qwen" in align_backend.lower()
            ass_content = generate_ass_script(
                canvas_w, canvas_h, segments_list, subtitle_style, font_size,
                bg_opacity, enable_highlight, is_qwen, margin_v=margin_v,
                enable_background=enable_background, background_color=bg_color,
            )
            ass_path = os.path.join(temp_dir, "subtitles.ass")
            with open(ass_path, "w", encoding="utf-8") as f:
                f.write(ass_content)

        out_mp4 = os.path.join(temp_dir, "output.mp4")

        local_fonts_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "frontend", "public", "vendor"))
        vf_chain = f"scale={canvas_w}:{canvas_h}:force_original_aspect_ratio=decrease,pad={canvas_w}:{canvas_h}:(ow-iw)/2:(oh-ih)/2"
        if subtitle_mode == "burn":
            ass_filter = f"ass={ass_path}"
            if os.path.isdir(local_fonts_dir):
                ass_filter += f":fontsdir={local_fonts_dir}"
            vf_chain += f",{ass_filter}"

        ffmpeg_cmd = [
            "ffmpeg", "-y",
            "-loop", "1", "-i", slide_path,
            "-i", audio_path,
            "-vf", vf_chain,
            "-c:v", "libx264",
            "-tune", "stillimage",
            "-c:a", "aac",
            "-shortest",
            "-pix_fmt", "yuv420p",
            out_mp4
        ]

        proc = await asyncio.create_subprocess_exec(
            *ffmpeg_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout_data, stderr_data = await proc.communicate()
        if proc.returncode != 0:
            err_text = (stderr_data or b"").decode(errors='ignore')
            logger.error(f"ASS FFmpeg Failed: {err_text}")
            detail = (err_text.strip().splitlines()[-1] if err_text.strip() else "ASS 渲染失敗")
            if len(detail) > 260:
                detail = detail[:260]
            raise HTTPException(status_code=500, detail=f"ASS 渲染失敗: {detail}")

        with open(out_mp4, "rb") as f:
            video_bytes = f.read()

        headers = {
            "Content-Disposition": "attachment; filename=subtitle_ass.mp4",
            "X-Subtitle-Render-Version": "ass-discrete-fast",
            "X-Subtitle-Style-Applied": subtitle_style,
        }
        if run_id and page_index >= 0:
            variant = get_video_run_store().record_page_variant(
                run_id=run_id,
                page_index=page_index,
                video_bytes=video_bytes,
                audio_bytes=None if tts_id else audio_bytes,
                slide_bytes=slide_bytes,
                segments=segments_list,
                ass_content=ass_content,
                settings={
                    "subtitle_style": subtitle_style,
                    "subtitle_output_mode": subtitle_mode,
                    "enable_highlight": enable_highlight,
                    "font_size": font_size,
                    "enable_background": enable_background,
                    "bg_color": bg_color,
                    "bg_opacity": bg_opacity,
                    "margin_v": margin_v,
                    "align_backend": align_backend,
                    "tts_id": tts_id,
                    "align_id": align_id or alignment_id,
                    "tts_voice": tts_voice,
                    "tts_speed": tts_speed,
                    "selected_voice_key": selected_voice_key,
                    "reference_text": reference_text,
                },
                label=variant_label or f"web-page-{page_index + 1}",
                variant_id=variant_id or tts_id or align_id,
            )
            headers["X-Variant-Id"] = variant.get("variant_id", "")

        return Response(
            content=video_bytes,
            media_type="video/mp4",
            headers=headers,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"ASS Render Error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if temp_dir:
            shutil.rmtree(temp_dir, ignore_errors=True)


@router.post("/api/video-abstract/merge-rendered-videos")
async def merge_rendered_videos(videos: List[UploadFile] = File(...)):
    """Merge already-rendered page videos in given order and return a downloadable MP4."""
    if not videos:
        raise HTTPException(status_code=400, detail="缺少影片片段")
    temp_dir = tempfile.mkdtemp(prefix="slideai_merge_")
    try:
        input_paths = []
        for i, up in enumerate(videos):
            name = up.filename or f"part_{i+1}.mp4"
            ext = os.path.splitext(name)[1] or ".mp4"
            p = os.path.join(temp_dir, f"part_{i:03d}{ext}")
            content = await up.read()
            with open(p, "wb") as f:
                f.write(content)
            input_paths.append(p)

        if not input_paths:
            raise HTTPException(status_code=400, detail="沒有可合併的片段")

        list_file = os.path.join(temp_dir, "concat.txt")
        with open(list_file, "w", encoding="utf-8") as f:
            for p in input_paths:
                safe_p = p.replace("'", "'\\''")
                f.write(f"file '{safe_p}'\n")

        out_path = os.path.join(temp_dir, "merged.mp4")
        ffmpeg_cmd = [
            "ffmpeg", "-y",
            "-f", "concat", "-safe", "0",
            "-i", list_file,
            "-c", "copy",
            out_path,
        ]
        proc = await asyncio.create_subprocess_exec(
            *ffmpeg_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_data, stderr_data = await proc.communicate()
        if proc.returncode != 0 or not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
            # fallback: re-encode for maximal compatibility
            out_path = os.path.join(temp_dir, "merged_reencode.mp4")
            ffmpeg_cmd2 = [
                "ffmpeg", "-y",
                "-f", "concat", "-safe", "0",
                "-i", list_file,
                "-c:v", "libx264",
                "-c:a", "aac",
                "-pix_fmt", "yuv420p",
                out_path,
            ]
            proc2 = await asyncio.create_subprocess_exec(
                *ffmpeg_cmd2,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _o2, e2 = await proc2.communicate()
            if proc2.returncode != 0 or not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
                err_text = ((stderr_data or b"").decode(errors="ignore") + "\n" + (e2 or b"").decode(errors="ignore")).strip()
                raise HTTPException(status_code=500, detail=f"影片合併失敗: {err_text[-300:]}")

        with open(out_path, "rb") as f:
            merged_bytes = f.read()
        return Response(
            content=merged_bytes,
            media_type="video/mp4",
            headers={"Content-Disposition": "attachment; filename=merged_rendered_preview.mp4"},
        )
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
