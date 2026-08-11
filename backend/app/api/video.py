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
import re
from pathlib import Path
from fastapi import APIRouter, UploadFile, File, Form, Request, Query, HTTPException, Response
from fastapi.responses import FileResponse, JSONResponse
import io
from pdf2image import convert_from_path
from pydantic import BaseModel, Field
from PIL import Image, UnidentifiedImageError
import urllib.parse
from typing import Optional, List
import pypdf as PyPDF2
from backend.app.services.speech_providers import (
    align_subtitles,
    get_tts_provider_name,
    synthesize_tts_preview,
    transcribe_reference_audio,
    tts_requires_reference_text,
    warm_tts_provider,
)
from backend.app.services.artifact_store import get_video_run_store
from backend.app.services.video_merge import merge_video_files
from backend.app.api.video_helpers import (
    apply_audio_speed as _apply_audio_speed,
    clamp_preview_speed as _clamp_preview_speed,
    is_local_only_mode as _is_local_only_mode,
    is_mock_mode as _is_mock_mode,
    is_truthy_env as _is_truthy_env,
    make_alignment_id as _make_alignment_id,
    pregenerate_thumbnails_safe as _pregenerate_thumbnails,
    split_user_script_to_pages as _split_user_script_to_pages,
    to_traditional_chinese_for_display as _to_traditional_chinese_for_display,
)
logger = logging.getLogger("video_abstract")
logging.basicConfig(level=logging.INFO)

router = APIRouter()
_ALIGNMENT_CACHE: dict[str, dict] = {}
_ALIGNMENT_CACHE_TTL_SEC = 900 # 15 minutes
MAX_CACHE_ENTRIES = 3
_PRESET_VOICE_DIR = Path(__file__).resolve().parents[1] / "static" / "ref_voices"
MAX_FILE_SIZE = int(os.getenv("MAX_FILE_SIZE", str(50 * 1024 * 1024)))


def _load_preset_reference_voice(voice_key: str) -> tuple[bytes, str, str] | None:
    """Resolve a built-in voice without trusting a client-supplied path."""
    key = str(voice_key or "").strip()
    if not key or key == "custom":
        return None
    try:
        manifest = json.loads((_PRESET_VOICE_DIR / "manifest.json").read_text(encoding="utf-8"))
        entry = manifest.get(key) or {}
        filename = Path(str(entry.get("file") or "")).name
        if not filename:
            return None
        audio_path = (_PRESET_VOICE_DIR / filename).resolve()
        if _PRESET_VOICE_DIR.resolve() not in audio_path.parents or not audio_path.is_file():
            return None
        return audio_path.read_bytes(), filename, str(entry.get("transcript") or "")
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


def _purge_alignment_cache() -> None:
    now = time.time()
    expired = [k for k, v in _ALIGNMENT_CACHE.items() if now - float(v.get("ts", 0)) > _ALIGNMENT_CACHE_TTL_SEC]
    for k in expired:
        _ALIGNMENT_CACHE.pop(k, None)

    if len(_ALIGNMENT_CACHE) > MAX_CACHE_ENTRIES:
        sorted_keys = sorted(_ALIGNMENT_CACHE.keys(), key=lambda k: float(_ALIGNMENT_CACHE[k].get("ts", 0)))
        for k in sorted_keys[:-MAX_CACHE_ENTRIES]:
            _ALIGNMENT_CACHE.pop(k, None)


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
            img = img.convert("RGB")
            max_w = 1280
            if img.width > max_w:
                ratio = max_w / max(img.width, 1)
                img = img.resize((max_w, max(1, int(img.height * ratio))), Image.Resampling.LANCZOS)
            thumbnail = io.BytesIO()
            img.save(thumbnail, format="JPEG", quality=82, optimize=True)
            try:
                # The store owns the per-run thread/process lock and verifies
                # that the manifest still exists.  Writing through it prevents
                # this daemon from recreating a run after the user deletes it.
                store.record_page_asset(
                    run_id=run_id,
                    page_index=page_idx,
                    slide_bytes=thumbnail.getvalue(),
                    suffix=".jpg",
                )
            except FileNotFoundError:
                logger.info(f"[UPLOAD][BG] Thumbnail cache cancelled for deleted run={run_id}")
                return
        logger.info(f"[UPLOAD][BG] Cached {len(images)} run thumbnails for run={run_id}")
    except Exception as thumb_err:
        logger.warning(f"[UPLOAD][BG] Run thumbnail cache failed run={run_id}: {thumb_err}")


class TextsRequest(BaseModel):
    texts: list[str]
    pdf_id: str
    resolution: int = 1080
    tts_model: str = 'voxcpm'
    voice: str = 'zh-TW-YunJheNeural'
    enable_subtitles: bool = True
    # 可擴充更多影片選項


def _use_nano_voxcpm_tts() -> bool:
    return get_tts_provider_name() in {"voxcpm_nano", "nano_vllm", "voxcpm"}


@router.post("/api/video-abstract")
async def video_abstract_api(
    request: Request,
    file: UploadFile = File(None),
):
    """
    1. 若有 file，回傳 AI 文字陣列（JSON，不產生影片）
    2. 若為 application/json 且有 texts，產生影片並回傳影片檔案
    """

    mock_mode = _is_mock_mode()
    local_only_mode = _is_local_only_mode()

    if file:
        # The local-first application accepts PDF presentations only.
        if file.content_type and file.content_type.startswith("video/"):
            raise HTTPException(status_code=415, detail="目前僅支援 PDF 簡報")

        # Reserve the persistent run input path up front so the run copy is the
        # only canonical project PDF.
        file.file.seek(0, os.SEEK_END)
        pdf_size = file.file.tell()
        file.file.seek(0)
        if pdf_size <= 0:
            raise HTTPException(status_code=400, detail="PDF 檔案是空的")
        if pdf_size > MAX_FILE_SIZE:
            raise HTTPException(
                status_code=413,
                detail=f"PDF 檔案過大，請上傳 {MAX_FILE_SIZE // 1024 // 1024}MB 以下的檔案",
            )
        pdf_signature = file.file.read(5)
        file.file.seek(0)
        if pdf_signature != b"%PDF-":
            raise HTTPException(status_code=400, detail="檔案內容不是有效的 PDF")

        pdf_id = str(uuid.uuid4())
        run_store = get_video_run_store()
        reserved_run_id = run_store.new_run_id()
        project_name = file.filename or "source.pdf"
        pdf_name = run_store.safe_filename(project_name, "source.pdf")
        if not pdf_name.lower().endswith(".pdf"):
            pdf_name += ".pdf"
        run_input_dir = run_store.run_dir(reserved_run_id) / "input"
        run_input_dir.mkdir(parents=True, exist_ok=True)
        pdf_path = str(run_input_dir / pdf_name)
        logger.info(f"[PDF UPLOAD] Saving canonical run PDF to {pdf_path}")
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

        try:
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
                from backend.app.services.utility.api import (
                    generate_presentation_scripts,
                    get_llm_api_key,
                    llm_is_configured,
                )
                script = "請根據每一頁內容生成簡報稿，語氣簡潔明確。"
                from dotenv import load_dotenv
                dotenv_path = os.path.join(os.path.dirname(__file__), '..', '.env')
                load_dotenv(dotenv_path=dotenv_path)
                api_key = get_llm_api_key()
                if not llm_is_configured():
                    raise RuntimeError(".env 尚未完成 LLM provider、model 或 endpoint 設定")
                # 優先使用 content_language，其次 language，再用 voice 提示
                detected_language = content_language or language_hint or voice_hint
                logger.info(f"[UPLOAD] Detected language for AI generation: {detected_language}")
                ai_texts = await generate_presentation_scripts(
                    text_array=text_array,
                    script=script,
                    api_key=api_key,
                    language=detected_language,
                )
        except Exception:
            shutil.rmtree(run_store.run_dir(reserved_run_id), ignore_errors=True)
            raise

        # Filesystem manifests are the sole project record.
        project_id = None

        run_manifest = None
        run_id = None
        try:
            run_manifest = run_store.create_run(
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
                run_id=reserved_run_id,
            )
            run_id = str(run_manifest.get("run_id") or "")
            logger.info(f"[UPLOAD] Created video run id={run_id} name={project_name}")
            if not _is_truthy_env("VIDEO_ABSTRACT_DISABLE_PERSISTENT_THUMBNAILS", "true"):
                thumb_base = os.path.join(os.path.dirname(__file__), "..", "user_thumbnails")
                thumb_base = os.path.abspath(thumb_base)
                thumb_dir = os.path.join(thumb_base, pdf_id)
                threading.Thread(
                    target=_pregenerate_thumbnails,
                    args=(pdf_path, thumb_dir, logger),
                    daemon=True,
                ).start()
            threading.Thread(
                target=_pregenerate_run_thumbnails_safe,
                args=(run_id, pdf_path),
                daemon=True,
            ).start()
        except Exception as run_err:
            logger.error(f"[UPLOAD] Failed to create persistent video run: {run_err}", exc_info=True)
            shutil.rmtree(run_store.run_dir(reserved_run_id), ignore_errors=True)
            raise HTTPException(status_code=500, detail=f"建立專案資料失敗: {run_err}")

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
async def video_abstract_thumbnail(pdf_id: str = Query(...), page: int = Query(1, ge=1)):
    """Return a PNG thumbnail for a given PDF page.

    Resolution order:
    1. user_thumbnails/<pdf_id>/page_N.png  (persistent, generated at upload)
    2. data/video_runs/<run_id>/pages cached image
    3. data/video_runs/<run_id>/input PDF rendered on demand

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

    # Resolve the legacy pdf_id through the canonical video-run manifest.
    store = get_video_run_store()
    manifest = store.find_manifest_by_pdf_id(pdf_id)
    if not manifest:
        logger.warning(f"[THUMBNAIL] No video run found for pdf_id={pdf_id}")
        raise HTTPException(status_code=404, detail="Thumbnail not found")

    run_id = str(manifest.get("run_id") or "")
    page_index = max(0, int(page) - 1)
    page_items = manifest.get("pages") or []
    page_item = page_items[page_index] if page_index < len(page_items) else {}
    candidates = []
    stored_slide = str(((page_item or {}).get("paths") or {}).get("slide") or "").strip()
    if stored_slide:
        candidates.append(stored_slide)
    page_dir = store.page_dir(run_id, page_index)
    candidates.extend([
        str(page_dir / f"page_{page:03d}.jpg"),
        str(page_dir / f"page_{page:03d}.png"),
        str(store.run_dir(run_id) / "pages" / f"page_{page:03d}.jpg"),
    ])
    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            media_type = "image/png" if candidate.lower().endswith(".png") else "image/jpeg"
            return FileResponse(candidate, media_type=media_type)

    pdf_path = str((manifest.get("paths") or {}).get("pdf") or "").strip()
    logger.info(f"[THUMBNAIL] Cached image not found; rendering run PDF: {pdf_path} page={page}")
    if not pdf_path or not os.path.isfile(pdf_path):
        logger.warning(f"[THUMBNAIL] Run PDF not found: {pdf_path}")
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
    out_fd, out_tmp = tempfile.mkstemp(prefix="slideai_tts_preview_", suffix=".mp3")
    os.close(out_fd)

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
    selected_voice_key: str = Form(""),
    response_mode: str = Form("audio"),
):
    """Generate one page TTS, persist it immediately, and return the audio."""
    import os
    if page_index < 0:
        raise HTTPException(status_code=400, detail="page_index must be >= 0")
    ref_data = await reference_audio.read() if reference_audio is not None else None
    reference_filename = reference_audio.filename if reference_audio is not None else "reference.wav"
    current_settings = {}
    if not ref_data:
        try:
            manifest = get_video_run_store().load_manifest(run_id)
            current_settings = ((manifest.get("settings") or {}).get("current") or {})
            saved_ref = current_settings.get("reference_audio") or {}
            saved_ref_path = Path(str(saved_ref.get("path") or ""))
            if saved_ref_path.is_file():
                ref_data = await asyncio.to_thread(saved_ref_path.read_bytes)
                reference_filename = str(saved_ref.get("filename") or saved_ref_path.name)
        except FileNotFoundError:
            pass
    if not ref_data:
        # Batch rendering submits JSON and cannot attach the browser File on
        # every page.  Resolve built-in voices directly from the trusted
        # server manifest, using either the request or persisted selection.
        preset = await asyncio.to_thread(
            _load_preset_reference_voice,
            selected_voice_key or current_settings.get("selected_voice_key") or "",
        )
        if preset:
            ref_data, reference_filename, preset_transcript = preset
            if not (reference_text or "").strip():
                reference_text = str(current_settings.get("reference_text") or preset_transcript)
    if not ref_data:
        raise HTTPException(status_code=400, detail="請提供參考音檔以生成語音。")
    file_suffix = os.path.splitext(reference_filename or "")[-1] or ".wav"
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
    variant = get_video_run_store().record_page_variant_tts(
        run_id=run_id,
        page_index=page_index,
        audio_source_path=final_out,
        metadata={
            "text": text,
            "voice": voice,
            "speed": speed,
            # Keep the server-side voice identity with the variant.  This is
            # also the reliable fallback for a later chunk regeneration when
            # the browser no longer has the original File object.
            "selected_voice_key": selected_voice_key or current_settings.get("selected_voice_key") or "",
            "reference_text": reference_text or "",
        },
        label=f"web-page-{page_index + 1}",
    )
    stored_audio_path = variant["paths"]["audio"]
    for temporary_path in {str(out_wav), str(final_out)}:
        try:
            if Path(temporary_path).resolve() != Path(stored_audio_path).resolve():
                Path(temporary_path).unlink(missing_ok=True)
                shutil.rmtree(Path(temporary_path).with_suffix(".chunks"), ignore_errors=True)
        except Exception:
            pass
    if str(response_mode or "").strip().lower() == "json":
        return JSONResponse({
            "ok": True,
            "tts_id": variant["variant_id"],
            "variant_id": variant["variant_id"],
            "audio_url": f"/api/video-runs/{run_id}/pages/{page_index}/variants/{variant['variant_id']}/audio",
            "chunks": [
                {k: value for k, value in chunk.items() if k != "path"}
                for chunk in ((variant.get("tts") or {}).get("chunks") or [])
            ],
        })
    return FileResponse(
        stored_audio_path,
        media_type="audio/wav",
        filename=f"page_{page_index + 1}_tts.wav",
        headers={
            "X-TTS-Id": variant["variant_id"],
            "X-Variant-Id": variant["variant_id"],
        },
    )


@router.post("/api/video-runs/{run_id}/pages/{page_index}/variants/{variant_id}/chunks/{chunk_index}/regenerate")
async def regenerate_video_run_tts_chunk(
    run_id: str,
    page_index: int,
    variant_id: str,
    chunk_index: int,
    text: str = Form(""),
):
    """Regenerate one existing four-sentence chunk and create a new page variant."""
    from backend.app.services.tts_chunks import combine_tts_chunks

    store = get_video_run_store()
    try:
        manifest = store.load_manifest(run_id)
        variant = store.get_page_variant(
            run_id=run_id, page_index=page_index, variant_id=variant_id,
        )
    except (FileNotFoundError, IndexError):
        raise HTTPException(status_code=404, detail="找不到要修正的語音變體")

    tts_data = variant.get("tts") or {}
    chunks = list(tts_data.get("chunks") or [])
    if chunk_index < 0 or chunk_index >= len(chunks):
        raise HTTPException(status_code=404, detail="此語音沒有可局部重生的四句分段")
    for chunk in chunks:
        if not Path(str(chunk.get("path") or "")).is_file():
            raise HTTPException(status_code=409, detail="舊語音沒有保留完整 chunk，請先重新生成整頁")

    current_settings = ((manifest.get("settings") or {}).get("current") or {})
    reference = current_settings.get("reference_audio") or {}
    reference_path = Path(str(reference.get("path") or ""))
    reference_bytes: bytes | None = None
    reference_filename = "reference.wav"
    preset_transcript = ""
    if reference_path.is_file():
        reference_bytes = await asyncio.to_thread(reference_path.read_bytes)
        reference_filename = str(reference.get("filename") or reference_path.name)
    else:
        # Preset voices intentionally do not create a per-project uploaded
        # reference file.  The ordinary page-TTS route already resolves them
        # from the trusted server manifest; chunk regeneration must use the
        # exact same fallback rather than incorrectly requiring an upload.
        metadata_voice_key = str((tts_data.get("metadata") or {}).get("selected_voice_key") or "")
        preset = await asyncio.to_thread(
            _load_preset_reference_voice,
            metadata_voice_key or current_settings.get("selected_voice_key") or "",
        )
        if preset:
            reference_bytes, reference_filename, preset_transcript = preset
    if not reference_bytes:
        raise HTTPException(
            status_code=400,
            detail="找不到此語音變體的參考音檔或內建音色；請在語音設定重新選擇音色後再重生。",
        )
    reference_text = str(
        (tts_data.get("metadata") or {}).get("reference_text")
        or current_settings.get("reference_text")
        or preset_transcript
        or ""
    ).strip()
    if tts_requires_reference_text() and not reference_text:
        raise HTTPException(status_code=400, detail="此語音模型需要參考音檔逐字稿")

    replacement_text = str(text or chunks[chunk_index].get("text") or "").strip()
    if not replacement_text:
        raise HTTPException(status_code=400, detail="重生文字不可為空")
    ok, replacement_path, reason = await asyncio.to_thread(
        synthesize_tts_preview,
        text=replacement_text,
        reference_audio_bytes=reference_bytes,
        reference_suffix=Path(reference_filename).suffix or ".wav",
        reference_text=reference_text,
    )
    if not ok or not replacement_path or not os.path.isfile(replacement_path):
        raise HTTPException(status_code=500, detail=f"局部 TTS 重生失敗：{reason}")

    workspace = tempfile.mkdtemp(prefix="slideai_chunk_regen_")
    try:
        combined_path = Path(workspace) / "combined.wav"
        sources = []
        for index, chunk in enumerate(chunks):
            chunk_text = replacement_text if index == chunk_index else str(chunk.get("text") or "")
            chunk_path = replacement_path if index == chunk_index else str(chunk.get("path") or "")
            sources.append((chunk_text, chunk_path))
        combine_tts_chunks(
            sources,
            combined_path,
            silence_ms=float(tts_data.get("chunk_silence_ms") or 120.0),
        )
        metadata = dict(tts_data.get("metadata") or {})
        metadata.update({
            "regenerated_from_variant_id": variant_id,
            "regenerated_chunk_index": chunk_index,
        })
        new_variant = store.record_page_variant_tts(
            run_id=run_id,
            page_index=page_index,
            audio_source_path=combined_path,
            metadata=metadata,
            label=f"chunk-{chunk_index + 1}-retry",
        )
        return JSONResponse({
            "ok": True,
            "tts_id": new_variant["variant_id"],
            "variant_id": new_variant["variant_id"],
            "audio_url": f"/api/video-runs/{run_id}/pages/{page_index}/variants/{new_variant['variant_id']}/audio",
            "chunks": [
                {key: value for key, value in chunk.items() if key != "path"}
                for chunk in ((new_variant.get("tts") or {}).get("chunks") or [])
            ],
            # If the user edited this local segment, the subsequent full-page
            # forced alignment must receive the same script as the combined
            # audio.  Returning it avoids a subtle audio/text timeline drift.
            "page_text": "\n\n".join(text for text, _ in sources).strip(),
        })
    finally:
        shutil.rmtree(workspace, ignore_errors=True)
        try:
            Path(replacement_path).unlink(missing_ok=True)
            shutil.rmtree(Path(replacement_path).with_suffix(".chunks"), ignore_errors=True)
        except Exception:
            pass


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
    audio_file: Optional[UploadFile] = File(None),
):
    """Align one page audio, persist segments immediately, and return them."""
    if page_index < 0:
        raise HTTPException(status_code=400, detail="page_index must be >= 0")
    target_variant_id = variant_id or tts_id
    if not target_variant_id:
        raise HTTPException(status_code=400, detail="variant_id is required for persistent alignment")
    if audio_file is not None and audio_file.filename:
        audio_bytes = await audio_file.read()
        audio_filename = audio_file.filename or "audio.wav"
        audio_source_path = None
    else:
        try:
            audio_path = get_video_run_store().get_variant_audio_path(
                run_id=run_id, page_index=page_index, variant_id=target_variant_id,
            )
        except (FileNotFoundError, IndexError):
            raise HTTPException(status_code=404, detail="找不到此變體的 TTS 音訊")
        audio_bytes = None
        audio_filename = audio_path.name
        audio_source_path = str(audio_path)
    result = await asyncio.to_thread(
        align_subtitles,
        text=text,
        audio_bytes=audio_bytes,
        audio_source_path=audio_source_path,
        audio_filename=audio_filename,
        language=language,
        alignment_mode=alignment_mode,
        split_min_chars=split_min_chars,
        split_max_chars=split_max_chars,
        enable_pause_split=enable_pause_split,
        pause_threshold_ms=pause_threshold_ms,
    )
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
            "warning": result.warning or "",
            "match_ratio": result.match_ratio,
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
            "warning": result.warning or "",
            "match_ratio": result.match_ratio,
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
                "warning": result.warning or "",
                "match_ratio": result.match_ratio,
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
    audio_file: Optional[UploadFile] = File(None),
    slide_image: Optional[UploadFile] = File(None),
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
        temp_dir = tempfile.mkdtemp(prefix="slideai_ass_temp_")
        store = get_video_run_store()
        target_variant_id = str(variant_id or tts_id or align_id or "").strip()
        persistent_request = bool(run_id and page_index >= 0 and target_variant_id)

        audio_bytes = None
        if persistent_request:
            try:
                audio_path = str(store.get_variant_audio_path(
                    run_id=run_id, page_index=page_index, variant_id=target_variant_id,
                ))
            except (FileNotFoundError, IndexError):
                raise HTTPException(status_code=404, detail="找不到此變體的 TTS 音訊")
        else:
            if audio_file is None or not audio_file.filename:
                raise HTTPException(status_code=400, detail="請上傳音檔")
            audio_bytes = await audio_file.read()
            audio_suffix = os.path.splitext(audio_file.filename or "audio.wav")[1] or ".wav"
            audio_path = os.path.join(temp_dir, "audio" + audio_suffix)
            with open(audio_path, "wb") as f:
                f.write(audio_bytes)

        slide_bytes = None
        if persistent_request:
            try:
                slide_path = str(store.get_page_slide_path(run_id=run_id, page_index=page_index))
            except (FileNotFoundError, IndexError):
                raise HTTPException(status_code=404, detail="找不到本頁投影片背景")
        else:
            slide_path = os.path.join(temp_dir, "slide.png")
            if slide_image and slide_image.filename:
                slide_bytes = await slide_image.read()
                with open(slide_path, "wb") as f:
                    f.write(slide_bytes)
            else:
                from PIL import Image
                Image.new('RGB', (1920, 1080), color=(0, 255, 0)).save(slide_path)

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

        canvas_w, canvas_h = 1920, 1080

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

        headers = {
            "Content-Disposition": "attachment; filename=subtitle_ass.mp4",
            "X-Subtitle-Render-Version": "ass-discrete-fast",
            "X-Subtitle-Style-Applied": subtitle_style,
        }
        persisted_video_path = ""
        if run_id and page_index >= 0:
            variant = store.record_page_variant(
                run_id=run_id,
                page_index=page_index,
                video_source_path=out_mp4,
                audio_bytes=None if tts_id else audio_bytes,
                slide_bytes=None if persistent_request else slide_bytes,
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
                variant_id=target_variant_id,
            )
            headers["X-Variant-Id"] = variant.get("variant_id", "")
            persisted_video_path = str((variant.get("paths") or {}).get("video") or "")

        if persisted_video_path and os.path.isfile(persisted_video_path):
            return FileResponse(persisted_video_path, media_type="video/mp4", headers=headers)

        with open(out_mp4, "rb") as f:
            video_bytes = f.read()

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


class BatchRenderJobRequest(BaseModel):
    page_indexes: List[int] = Field(default_factory=list)
    subtitle_mode: str = "burn"
    split_min_chars: int = 10
    split_max_chars: int = 32
    tts_voice: str = ""
    tts_speed: float = 1.0
    selected_voice_key: str = ""
    reference_text: str = ""
    subtitle_settings: dict = Field(default_factory=dict)
    auto_merge: bool = False
    transitions_enabled: bool = False


class AgentVideoJobConfig(BaseModel):
    """Stable, intentionally small contract for unattended PDF rendering."""

    scripts: List[str]
    reference_text: str
    label: str = ""
    subtitle_mode: str = "burn"  # none | srt | burn
    tts_speed: float = Field(default=1.0, ge=0.5, le=2.0)
    selected_voice_key: str = "custom"
    split_min_chars: int = Field(default=10, ge=4, le=80)
    split_max_chars: int = Field(default=32, ge=8, le=120)
    transitions_enabled: bool = False
    subtitle_settings: dict = Field(default_factory=dict)


@router.post("/api/agent/video-jobs", status_code=202)
async def create_agent_video_job(
    pdf: UploadFile = File(...),
    reference_audio: UploadFile = File(...),
    config_json: str = Form(...),
):
    """Submit PDF + reference voice + JSON and run the full video pipeline.

    The caller supplies exactly one script per PDF page.  The endpoint creates
    a persistent run, renders it through the shared FIFO GPU queue, and merges
    the selected page videos automatically.  It therefore behaves exactly like
    the WebUI pipeline without requiring a browser session.
    """
    try:
        config = AgentVideoJobConfig.model_validate_json(config_json)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"config_json 格式錯誤：{exc}")

    subtitle_mode = str(config.subtitle_mode or "burn").strip().lower()
    if subtitle_mode not in {"none", "srt", "burn"}:
        raise HTTPException(status_code=422, detail="subtitle_mode 必須是 none、srt 或 burn")
    if config.split_min_chars > config.split_max_chars:
        raise HTTPException(status_code=422, detail="split_min_chars 不可大於 split_max_chars")
    if tts_requires_reference_text() and not config.reference_text.strip():
        raise HTTPException(status_code=422, detail="VoxCPM2 語音克隆需要 reference_text")

    pdf_bytes = await pdf.read()
    if not pdf_bytes or not pdf_bytes.startswith(b"%PDF-"):
        raise HTTPException(status_code=400, detail="pdf 必須是有效的 PDF 檔案")
    if len(pdf_bytes) > MAX_FILE_SIZE:
        raise HTTPException(status_code=413, detail=f"PDF 不可超過 {MAX_FILE_SIZE // 1024 // 1024}MB")
    reference_bytes = await reference_audio.read()
    if not reference_bytes:
        raise HTTPException(status_code=400, detail="reference_audio 不可為空")

    try:
        page_count = len(PyPDF2.PdfReader(io.BytesIO(pdf_bytes)).pages)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"PDF 解析失敗：{exc}")
    scripts = [str(item or "").strip() for item in config.scripts]
    if len(scripts) != page_count:
        raise HTTPException(
            status_code=422,
            detail=f"scripts 必須與 PDF 頁數一致（PDF={page_count}，scripts={len(scripts)}）",
        )
    empty_pages = [index + 1 for index, text in enumerate(scripts) if not text]
    if empty_pages:
        raise HTTPException(status_code=422, detail=f"scripts 不可留空；空白頁：{empty_pages[:20]}")

    store = get_video_run_store()
    workspace = tempfile.mkdtemp(prefix="slideai_agent_submit_")
    try:
        source_path = Path(workspace) / "source.pdf"
        source_path.write_bytes(pdf_bytes)
        manifest = store.create_run(
            pdf_path=source_path,
            original_filename=pdf.filename or "agent-input.pdf",
            scripts=scripts,
            settings={"agent_request": config.model_dump()},
            source="agent-api",
        )
        run_id = str(manifest["run_id"])
        store.update_settings(
            run_id,
            {
                "selected_voice_key": config.selected_voice_key,
                "reference_text": config.reference_text,
                "tts_speed": config.tts_speed,
                "subtitle_mode": subtitle_mode,
                "subtitle": config.subtitle_settings,
                "has_reference_audio": True,
            },
            reference_audio=reference_bytes,
            reference_audio_name=reference_audio.filename or "reference.wav",
        )
        # The browser normally has time to create these while the user edits
        # scripts.  An agent submits immediately, so prepare all page images
        # before allowing the GPU job to start and avoid an asset race.
        persisted_pdf = str((store.load_manifest(run_id).get("paths") or {}).get("pdf") or "")
        await asyncio.to_thread(_pregenerate_run_thumbnails_safe, run_id, persisted_pdf)
        job = store.create_job(
            run_id=run_id,
            payload=BatchRenderJobRequest(
                page_indexes=list(range(page_count)),
                subtitle_mode=subtitle_mode,
                split_min_chars=config.split_min_chars,
                split_max_chars=config.split_max_chars,
                tts_speed=config.tts_speed,
                selected_voice_key=config.selected_voice_key,
                reference_text=config.reference_text,
                subtitle_settings=config.subtitle_settings,
                auto_merge=True,
                transitions_enabled=config.transitions_enabled,
            ).model_dump(),
        )
        job_id = str(job["job_id"])
        _start_batch_job_task(run_id, job_id)
        return JSONResponse(
            {
                "run_id": run_id,
                "job_id": job_id,
                "status": "queued",
                "status_url": f"/api/agent/video-jobs/{run_id}/{job_id}",
                "cancel_url": f"/api/video-runs/{run_id}/jobs/{job_id}/cancel",
            },
            status_code=202,
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("[Agent API] submit failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=f"建立 Agent 影片任務失敗：{exc}")
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


_BATCH_JOB_TASKS: dict[str, asyncio.Task] = {}
_BATCH_GPU_QUEUE_LOCK = asyncio.Lock()
_BATCH_WAITING_ORDER: list[tuple[str, str]] = []
_BATCH_ACTIVE_JOB: tuple[str, str] | None = None


def _register_batch_waiter(run_id: str, job_id: str) -> None:
    item = (str(run_id), str(job_id))
    if item != _BATCH_ACTIVE_JOB and item not in _BATCH_WAITING_ORDER:
        _BATCH_WAITING_ORDER.append(item)


def _unregister_batch_waiter(run_id: str, job_id: str) -> None:
    item = (str(run_id), str(job_id))
    try:
        _BATCH_WAITING_ORDER.remove(item)
    except ValueError:
        pass


def _public_job_progress(job: dict | None) -> dict:
    job = job or {}
    return {
        "stage": str(job.get("stage") or "queued"),
        "stage_index": int(job.get("stage_index") or 0),
        "stage_total": int(job.get("stage_total") or 0),
        "current_page_index": job.get("current_page_index"),
    }


def _batch_queue_metadata(run_id: str, job_id: str) -> dict:
    item = (str(run_id), str(job_id))
    active = _BATCH_ACTIVE_JOB
    if item == active:
        state = "running"
        waiting_position = 0
        jobs_ahead = 0
    else:
        try:
            waiting_index = _BATCH_WAITING_ORDER.index(item)
        except ValueError:
            waiting_index = -1
        state = "queued" if waiting_index >= 0 else "finished"
        waiting_position = waiting_index + 1 if waiting_index >= 0 else 0
        jobs_ahead = waiting_index + (1 if active else 0) if waiting_index >= 0 else 0

    active_progress = None
    if active:
        try:
            active_progress = _public_job_progress(
                get_video_run_store().load_job(run_id=active[0], job_id=active[1])
            )
        except Exception:
            active_progress = {"stage": "running", "stage_index": 0, "stage_total": 0, "current_page_index": None}
    return {
        "queue_state": state,
        "queue_position": waiting_position,
        "jobs_ahead": max(0, jobs_ahead),
        "waiting_jobs": len(_BATCH_WAITING_ORDER),
        "active": active_progress,
    }


def _job_cancel_requested(run_id: str, job_id: str) -> bool:
    try:
        return bool(get_video_run_store().load_job(run_id=run_id, job_id=job_id).get("cancel_requested"))
    except Exception:
        return True


async def _run_persistent_batch_job(run_id: str, job_id: str) -> None:
    global _BATCH_ACTIVE_JOB
    store = get_video_run_store()
    try:
        if _job_cancel_requested(run_id, job_id):
            raise asyncio.CancelledError
        async with _BATCH_GPU_QUEUE_LOCK:
            _unregister_batch_waiter(run_id, job_id)
            _BATCH_ACTIVE_JOB = (str(run_id), str(job_id))
            job = store.load_job(run_id=run_id, job_id=job_id)
            payload = job.get("payload") or {}
            manifest = store.load_manifest(run_id)
            pages = manifest.get("pages") or []
            requested = payload.get("page_indexes") or []
            page_indexes = []
            for raw_index in requested:
                try:
                    index = int(raw_index)
                except (TypeError, ValueError):
                    continue
                if 0 <= index < len(pages) and str(pages[index].get("script") or "").strip() and index not in page_indexes:
                    page_indexes.append(index)
            if not page_indexes:
                raise ValueError("沒有具備講稿的可渲染頁面")

            store.update_job(
                run_id=run_id, job_id=job_id,
                updates={
                    "status": "running", "stage": "tts", "stage_index": 0,
                    "stage_total": len(page_indexes), "current_page_index": None, "error": "",
                },
            )

            # Stage 1: all TTS. Completed page artifacts are reused on resume.
            for order, page_index in enumerate(page_indexes, start=1):
                if _job_cancel_requested(run_id, job_id):
                    raise asyncio.CancelledError
                state = (store.load_job(run_id=run_id, job_id=job_id).get("pages") or {}).get(str(page_index), {})
                variant_id = str(state.get("variant_id") or "")
                try:
                    if variant_id:
                        store.get_variant_audio_path(run_id=run_id, page_index=page_index, variant_id=variant_id)
                    else:
                        raise FileNotFoundError
                except (FileNotFoundError, IndexError):
                    response = await video_run_page_tts_endpoint(
                        run_id=run_id,
                        page_index=page_index,
                        text=str(pages[page_index].get("script") or ""),
                        voice=str(payload.get("tts_voice") or ""),
                        speed=float(payload.get("tts_speed") or 1.0),
                        reference_audio=None,
                        reference_text=str(payload.get("reference_text") or ""),
                        selected_voice_key=str(payload.get("selected_voice_key") or ""),
                        response_mode="json",
                    )
                    data = json.loads(bytes(response.body).decode("utf-8"))
                    variant_id = str(data.get("variant_id") or "")
                store.update_job(
                    run_id=run_id, job_id=job_id,
                    updates={
                        "stage_index": order,
                        "stage_total": len(page_indexes),
                        "current_page_index": page_index,
                        "pages": {str(page_index): {
                            **state, "variant_id": variant_id, "status": "tts_ready",
                            "stage_index": order, "stage_total": len(page_indexes),
                        }},
                    },
                )

            try:
                from backend.app.services.voxtts import release_voxtts_worker
                release_voxtts_worker()
            except Exception:
                pass

            subtitle_mode = str(payload.get("subtitle_mode") or "burn").lower()
            if subtitle_mode != "none":
                store.update_job(
                    run_id=run_id,
                    job_id=job_id,
                    updates={"stage": "alignment", "stage_index": 0, "stage_total": len(page_indexes), "current_page_index": None},
                )
                for order, page_index in enumerate(page_indexes, start=1):
                    if _job_cancel_requested(run_id, job_id):
                        raise asyncio.CancelledError
                    state = (store.load_job(run_id=run_id, job_id=job_id).get("pages") or {}).get(str(page_index), {})
                    variant_id = str(state.get("variant_id") or "")
                    variant = store.get_page_variant(run_id=run_id, page_index=page_index, variant_id=variant_id)
                    segments_path = Path(str((variant.get("paths") or {}).get("segments") or ""))
                    if segments_path.is_file() and state.get("status") in {"align_ready", "rendered"}:
                        segments = (json.loads(segments_path.read_text(encoding="utf-8")).get("segments") or [])
                        align_backend = str(((variant.get("alignment") or {}).get("metadata") or {}).get("backend") or "")
                        warning = str(((variant.get("alignment") or {}).get("metadata") or {}).get("warning") or "")
                    else:
                        response = await video_run_page_align_endpoint(
                            run_id=run_id,
                            page_index=page_index,
                            text=str(pages[page_index].get("script") or ""),
                            language="auto",
                            alignment_mode="auto",
                            split_min_chars=int(payload.get("split_min_chars") or 10),
                            split_max_chars=int(payload.get("split_max_chars") or 32),
                            enable_pause_split=False,
                            pause_threshold_ms=320,
                            tts_id=variant_id,
                            variant_id=variant_id,
                            audio_file=None,
                        )
                        data = json.loads(bytes(response.body).decode("utf-8"))
                        segments = data.get("segments") or []
                        align_backend = str(data.get("backend") or "")
                        warning = str(data.get("warning") or "")
                    store.update_job(
                        run_id=run_id, job_id=job_id,
                        updates={
                            "stage_index": order,
                            "stage_total": len(page_indexes),
                            "current_page_index": page_index,
                            "pages": {str(page_index): {
                                **state, "variant_id": variant_id, "status": "align_ready",
                                "align_backend": align_backend, "warning": warning,
                                "stage_index": order, "stage_total": len(page_indexes),
                            }},
                        },
                    )
            try:
                from backend.app.services.subtitle_alignment import release_alignment_worker
                release_alignment_worker()
            except Exception:
                pass

            store.update_job(
                run_id=run_id,
                job_id=job_id,
                updates={"stage": "render", "stage_index": 0, "stage_total": len(page_indexes), "current_page_index": None},
            )
            style = payload.get("subtitle_settings") or {}
            for order, page_index in enumerate(page_indexes, start=1):
                if _job_cancel_requested(run_id, job_id):
                    raise asyncio.CancelledError
                state = (store.load_job(run_id=run_id, job_id=job_id).get("pages") or {}).get(str(page_index), {})
                variant_id = str(state.get("variant_id") or "")
                try:
                    if state.get("status") == "rendered":
                        store.get_variant_video_path(run_id=run_id, page_index=page_index, variant_id=variant_id)
                        store.update_job(
                            run_id=run_id,
                            job_id=job_id,
                            updates={
                                "stage_index": order,
                                "stage_total": len(page_indexes),
                                "current_page_index": page_index,
                            },
                        )
                        continue
                except (FileNotFoundError, IndexError):
                    pass
                variant = store.get_page_variant(run_id=run_id, page_index=page_index, variant_id=variant_id)
                segments = []
                if subtitle_mode != "none":
                    segments_path = Path(str((variant.get("paths") or {}).get("segments") or ""))
                    if segments_path.is_file():
                        segments = json.loads(segments_path.read_text(encoding="utf-8")).get("segments") or []
                await render_subtitle_ass_video(
                    audio_file=None,
                    slide_image=None,
                    segments_json=json.dumps(segments, ensure_ascii=False),
                    alignment_id="",
                    subtitle_style="bg-dark",
                    subtitle_mode=subtitle_mode,
                    enable_highlight=bool(style.get("enable_highlight", False)),
                    font_size=int(style.get("font_size") or 52),
                    enable_background=bool(style.get("enable_background", True)),
                    bg_color=str(style.get("bg_color") or "#000000"),
                    bg_opacity=int(style.get("bg_opacity") or 55),
                    margin_v=int(style.get("margin_v") or 90),
                    align_backend=str(state.get("align_backend") or ""),
                    run_id=run_id,
                    page_index=page_index,
                    variant_label=f"batch-page-{page_index + 1}",
                    tts_id=variant_id,
                    align_id=variant_id if subtitle_mode != "none" else "",
                    variant_id=variant_id,
                    tts_voice=str(payload.get("tts_voice") or ""),
                    tts_speed=str(payload.get("tts_speed") or 1.0),
                    selected_voice_key=str(payload.get("selected_voice_key") or ""),
                    reference_text=str(payload.get("reference_text") or ""),
                )
                store.update_job(
                    run_id=run_id, job_id=job_id,
                    updates={
                        "stage_index": order,
                        "stage_total": len(page_indexes),
                        "current_page_index": page_index,
                        "pages": {str(page_index): {
                            **state, "variant_id": variant_id, "status": "rendered",
                            "stage_index": order, "stage_total": len(page_indexes),
                        }},
                    },
                )

            result: dict = {}
            if bool(payload.get("auto_merge")):
                store.update_job(
                    run_id=run_id,
                    job_id=job_id,
                    updates={
                        "stage": "merge", "stage_index": 0, "stage_total": 1,
                        "current_page_index": None,
                    },
                )
                latest_job = store.load_job(run_id=run_id, job_id=job_id)
                variant_ids = {
                    str(page_index): str(
                        ((latest_job.get("pages") or {}).get(str(page_index), {}) or {}).get("variant_id") or ""
                    )
                    for page_index in page_indexes
                }
                from backend.app.api.video_runs import merge_selected_video_run_variants

                merge_response = await merge_selected_video_run_variants(
                    run_id=run_id,
                    page_indexes_json=json.dumps(page_indexes),
                    variant_ids_json=json.dumps(variant_ids),
                    response_mode="json",
                    transitions_enabled=bool(payload.get("transitions_enabled")),
                )
                merge_data = json.loads(bytes(merge_response.body).decode("utf-8"))
                export_id = str(merge_data.get("export_variant_id") or "")
                if not export_id:
                    raise RuntimeError("自動合併完成但沒有 export_variant_id")
                result = {
                    "export_variant_id": export_id,
                    "video_url": f"/api/video-runs/{run_id}/exports/{export_id}/video",
                    "srt_url": (
                        f"/api/video-runs/{run_id}/exports/{export_id}/subtitles.srt"
                        if subtitle_mode != "none" else ""
                    ),
                    "bundle_url": f"/api/video-runs/{run_id}/exports/{export_id}/download.zip",
                }

            store.update_job(
                run_id=run_id, job_id=job_id,
                updates={
                    "status": "completed", "stage": "completed", "stage_index": len(page_indexes),
                    "stage_total": len(page_indexes), "current_page_index": None,
                    "cancel_requested": False, "result": result,
                },
            )
    except asyncio.CancelledError:
        store.update_job(
            run_id=run_id, job_id=job_id,
            updates={"status": "cancelled", "stage": "cancelled", "cancel_requested": True},
        )
    except Exception as exc:
        logger.error("[BatchJob] run=%s job=%s failed: %s", run_id, job_id, exc, exc_info=True)
        store.update_job(
            run_id=run_id, job_id=job_id,
            updates={"status": "failed", "stage": "failed", "error": str(exc)[:1000]},
        )
    finally:
        _unregister_batch_waiter(run_id, job_id)
        if _BATCH_ACTIVE_JOB == (str(run_id), str(job_id)):
            _BATCH_ACTIVE_JOB = None
        _BATCH_JOB_TASKS.pop(job_id, None)


def _start_batch_job_task(run_id: str, job_id: str) -> None:
    existing = _BATCH_JOB_TASKS.get(job_id)
    if existing and not existing.done():
        return
    _register_batch_waiter(run_id, job_id)
    _BATCH_JOB_TASKS[job_id] = asyncio.create_task(_run_persistent_batch_job(run_id, job_id))


def recover_persistent_batch_jobs() -> int:
    """Recover queued/interrupted jobs in their original creation order."""
    store = get_video_run_store()
    jobs = store.list_all_jobs(statuses={"queued", "running"})
    for job in jobs:
        run_id = str(job.get("run_id") or "")
        job_id = str(job.get("job_id") or "")
        if not run_id or not job_id:
            continue
        if job.get("status") == "running":
            store.update_job(
                run_id=run_id,
                job_id=job_id,
                updates={"status": "queued", "stage": "queued", "error": ""},
            )
        _start_batch_job_task(run_id, job_id)
    return len(jobs)


@router.post("/api/video-runs/{run_id}/jobs/render")
async def create_batch_render_job(run_id: str, request: BatchRenderJobRequest):
    store = get_video_run_store()
    try:
        for existing in store.list_jobs(run_id=run_id):
            if existing.get("status") in {"queued", "running"}:
                _start_batch_job_task(run_id, str(existing["job_id"]))
                return JSONResponse(existing, status_code=202)
        job = store.create_job(run_id=run_id, payload=request.model_dump())
        _start_batch_job_task(run_id, str(job["job_id"]))
        return JSONResponse(job, status_code=202)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Run not found")


@router.get("/api/video-runs/{run_id}/jobs/{job_id}")
async def get_batch_render_job(run_id: str, job_id: str):
    try:
        job = get_video_run_store().load_job(run_id=run_id, job_id=job_id)
        return JSONResponse({**job, "queue": _batch_queue_metadata(run_id, job_id)})
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Job not found")


@router.get("/api/agent/video-jobs/{run_id}/{job_id}")
async def get_agent_video_job(run_id: str, job_id: str):
    """Agent-oriented job status with stable artifact URLs on completion."""
    try:
        job = get_video_run_store().load_job(run_id=run_id, job_id=job_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Agent video job not found")
    result = dict(job.get("result") or {})
    return JSONResponse({
        "run_id": run_id,
        "job_id": job_id,
        "status": job.get("status"),
        "stage": job.get("stage"),
        "stage_index": job.get("stage_index", 0),
        "stage_total": job.get("stage_total", 0),
        "current_page_index": job.get("current_page_index"),
        "queue": _batch_queue_metadata(run_id, job_id),
        "error": job.get("error", ""),
        "result": result,
    })


@router.get("/api/video-runs/{run_id}/jobs-current")
async def get_current_batch_render_job(run_id: str):
    """Return this run's active/queued job so a refreshed UI can reattach."""
    try:
        jobs = get_video_run_store().list_jobs(run_id=run_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Run not found")
    for job in jobs:
        if job.get("status") in {"queued", "running"}:
            job_id = str(job.get("job_id") or "")
            return JSONResponse({**job, "queue": _batch_queue_metadata(run_id, job_id)})
    return Response(status_code=204)


@router.post("/api/video-runs/{run_id}/jobs/{job_id}/cancel")
async def cancel_batch_render_job(run_id: str, job_id: str):
    try:
        item = (str(run_id), str(job_id))
        task = _BATCH_JOB_TASKS.get(job_id)
        if item != _BATCH_ACTIVE_JOB:
            _unregister_batch_waiter(run_id, job_id)
            job = get_video_run_store().update_job(
                run_id=run_id,
                job_id=job_id,
                updates={"status": "cancelled", "stage": "cancelled", "cancel_requested": True},
            )
            if task and not task.done():
                task.cancel()
            _BATCH_JOB_TASKS.pop(job_id, None)
        else:
            # Active work stops safely at the next page boundary, avoiding a
            # half-written TTS/alignment/video artifact.
            job = get_video_run_store().update_job(
                run_id=run_id, job_id=job_id, updates={"cancel_requested": True},
            )
        return JSONResponse(job, status_code=202)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Job not found")


@router.post("/api/video-runs/{run_id}/jobs/{job_id}/resume")
async def resume_batch_render_job(run_id: str, job_id: str):
    try:
        job = get_video_run_store().update_job(
            run_id=run_id, job_id=job_id,
            updates={"status": "queued", "stage": "queued", "cancel_requested": False, "error": ""},
        )
        _start_batch_job_task(run_id, job_id)
        return JSONResponse(job, status_code=202)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Job not found")


@router.post("/api/video-abstract/merge-rendered-videos")
async def merge_rendered_videos(
    videos: List[UploadFile] = File(...),
    transitions_enabled: bool = Form(False),
):
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

        try:
            out_path, transition_metadata = await merge_video_files(
                input_paths,
                temp_dir,
                transitions_enabled=transitions_enabled,
            )
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=500, detail=str(exc))

        with open(out_path, "rb") as f:
            merged_bytes = f.read()
        return Response(
            content=merged_bytes,
            media_type="video/mp4",
            headers={
                "Content-Disposition": "attachment; filename=merged_rendered_preview.mp4",
                "X-Transition-Seed": str(transition_metadata.get("seed") or ""),
            },
        )
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
