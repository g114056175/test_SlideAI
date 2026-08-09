import os
import logging
import json
import asyncio
import tempfile
import shutil
import zipfile
from datetime import datetime
from typing import Optional, List
import re
import uuid
from urllib.parse import quote

import pypdf as PyPDF2
from fastapi import APIRouter, UploadFile, File, Form, Request, Query, HTTPException
from fastapi.responses import FileResponse, JSONResponse, Response
from starlette.background import BackgroundTask
from pdf2image import convert_from_path
from pydantic import BaseModel, Field
from PIL import Image

from backend.app.api.video_helpers import is_truthy_env
from backend.app.services.artifact_store import get_video_run_store
from backend.app.services.alignment.subtitle_builder import build_srt
from backend.app.services.video_merge import merge_video_files

logger = logging.getLogger("video_abstract")
router = APIRouter()


@router.get("/api/llm/status")
async def get_llm_status():
    """Return non-secret LLM readiness metadata for the script editor."""
    from backend.app.services.utility.api import get_llm_config_summary
    config = get_llm_config_summary()
    return JSONResponse({
        "configured": bool(config.get("configured")),
        "provider": str(config.get("provider") or "missing"),
        "model": str(config.get("model") or ""),
    })


def _safe_download_filename(value: str, fallback: str = "video", suffix: str = ".mp4") -> str:
    name = str(value or "").strip()
    name = re.sub(r"\.[Pp][Dd][Ff]$", "", name)
    name = re.sub(r'[\\/:*?"<>|]+', "_", name)
    name = re.sub(r"\s+", " ", name).strip() or fallback
    if not name.lower().endswith(suffix):
        name += suffix
    return name


def _content_disposition_attachment(filename: str) -> str:
    ascii_name = re.sub(r"[^A-Za-z0-9._ -]+", "_", filename).strip() or "video.mp4"
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}"


def _download_bundle(entries: list[tuple[str, str]], filename: str) -> FileResponse:
    """Create a temporary ZIP without recompressing already-compressed videos."""
    temp_dir = tempfile.mkdtemp(prefix="slideai_download_")
    zip_path = os.path.join(temp_dir, "download.zip")
    try:
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_STORED) as archive:
            for source_path, archive_name in entries:
                if source_path and os.path.isfile(source_path):
                    archive.write(source_path, arcname=archive_name)
        if not os.path.isfile(zip_path) or os.path.getsize(zip_path) == 0:
            raise RuntimeError("下載壓縮檔建立失敗")
        return FileResponse(
            zip_path,
            media_type="application/zip",
            filename=filename,
            background=BackgroundTask(shutil.rmtree, temp_dir, ignore_errors=True),
        )
    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise


async def _media_duration_seconds(path: str) -> float:
    proc = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", path,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, _stderr = await proc.communicate()
    try:
        return max(0.0, float((stdout or b"").decode().strip()))
    except (TypeError, ValueError):
        return 0.0


class LocalPdfRunRequest(BaseModel):
    pdf_path: str
    subtitle_source: str = "none"
    run_label: str = ""
    settings: dict = Field(default_factory=dict)
    scripts: list[str] = Field(default_factory=list)


class VideoRunUpdateRequest(BaseModel):
    display_name: Optional[str] = None


class VideoRunScriptsUpdateRequest(BaseModel):
    scripts: List[str]


class VideoRunGenerateScriptsRequest(BaseModel):
    pages: Optional[List[int]] = None
    scope: str = "all"  # all | current
    source: str = "pdf"  # reserved: keep explicit source in API contract
    language: str = "zh"
    overwrite: bool = True


def _normalize_script_text(raw: str) -> str:
    text = str(raw or "").replace("\r\n", "\n").replace("\r", "\n")
    text = _strip_any_page_tags(text)
    lines = [ln.strip() for ln in text.split("\n")]
    compact = []
    blank = False
    for ln in lines:
        if not ln:
            if not blank:
                compact.append("")
            blank = True
            continue
        compact.append(ln)
        blank = False
    out = "\n".join(compact).strip()
    out = out.replace("\n\n\n", "\n\n")
    return out


def _trim_redundant_opening(page_idx: int, raw: str) -> str:
    """Keep greeting on page 1 only; remove repeated opening greetings on later pages."""
    text = str(raw or "").strip()
    if page_idx <= 0 or not text:
        return text
    patterns = [
        r"^大家好[，,。]?\s*",
        r"^各位好[，,。]?\s*",
        r"^今天(?:我(?:們)?|要)?(?:想)?(?:跟|和)?各位(?:分享|介紹)[^。！？!?\n]*[。！？!?\n]\s*",
        r"^今天(?:我(?:們)?|要)?(?:想)?(?:跟|和)?大家(?:分享|介紹)[^。！？!?\n]*[。！？!?\n]\s*",
    ]
    out = text
    for p in patterns:
        out = re.sub(p, "", out, flags=re.IGNORECASE)
    return out.strip() or text


def _is_outline_page(text: str) -> bool:
    src = str(text or "").lower()
    if not src:
        return False
    hints = [
        "目錄", "大綱", "章節", "contents", "table of contents", "agenda", "outline",
    ]
    return any(h in src for h in hints)


def _looks_incomplete(text: str) -> bool:
    s = str(text or "").strip()
    if not s:
        return True
    if s.endswith(("：", ":", "、", "，", ",")):
        return True
    if len(s) < 18:
        return True
    return False


def _parse_tagged_pages(raw: str, requested: list[int]) -> dict[int, str]:
    out: dict[int, str] = {}
    src = str(raw or "")
    for page_idx in requested:
        page_no = page_idx + 1
        pattern = re.compile(
            rf"(?:^|\n)\s*#?\s*PAGE[_\-\s]*0*{page_no}\s*#?\s*\n?"
            rf"(.*?)"
            rf"(?=(?:^|\n)\s*#?\s*(?:END[_\-\s]*PAGE|ENDPAGE)[_\-\s]*0*{page_no}\s*#?|"
            rf"(?:^|\n)\s*#?\s*PAGE[_\-\s]*0*{page_no + 1}\s*#?|\Z)",
            flags=re.IGNORECASE | re.DOTALL,
        )
        match = pattern.search(src)
        if not match:
            continue
        body = match.group(1).strip()
        if body:
            out[page_idx] = _strip_any_page_tags(body)
    return out


def _strip_any_page_tags(raw: str) -> str:
    s = str(raw or "")
    s = re.sub(r"(?im)^\s*#?\s*PAGE[_\-\s]*\d+\s*#?\s*$", "", s)
    s = re.sub(r"(?im)^\s*#?\s*(?:END[_\-\s]*PAGE|ENDPAGE)[_\-\s]*\d+\s*#?\s*$", "", s)
    return s.strip()


def _split_bulk_script_to_pages(raw: str, requested: list[int]) -> dict[int, str]:
    """
    Best-effort parser when strict #PAGE_NNN# tags are missing.
    Supports:
    - 第1頁 / 第 1 頁 / Page 1 / Slide 1 markers
    - paragraph fallback (blank-line split)
    """
    src = str(raw or "").replace("\r\n", "\n").replace("\r", "\n")
    out: dict[int, str] = {}
    if not src.strip() or not requested:
        return out

    marker_pat = re.compile(
        r"(?:^|\n)\s*(?:第\s*(\d+)\s*頁|page\s*(\d+)|slide\s*(\d+))\s*[:：\-]?\s*",
        flags=re.IGNORECASE,
    )
    matches = list(marker_pat.finditer(src))
    if matches:
        for i, m in enumerate(matches):
            page_no = int((m.group(1) or m.group(2) or m.group(3) or "0").strip() or "0")
            if page_no <= 0:
                continue
            page_idx = page_no - 1
            start = m.end()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(src)
            body = _normalize_script_text(src[start:end])
            if page_idx in requested and body:
                out[page_idx] = body
        if out:
            return out

    chunks = [x.strip() for x in re.split(r"\n\s*\n+", src) if x.strip()]
    if not chunks:
        one = _normalize_script_text(src)
        if one:
            out[requested[0]] = one
        return out

    if len(chunks) >= len(requested):
        for i, page_idx in enumerate(requested):
            if i < len(chunks):
                out[page_idx] = _normalize_script_text(chunks[i])
        return out

    one = _normalize_script_text(src)
    if one:
        for page_idx in requested:
            out[page_idx] = one
    return out


@router.get("/api/video-runs")
async def list_video_runs(limit: int = Query(50, ge=1, le=200)):
    """List persistent PDF-to-video run records."""
    return JSONResponse({"runs": get_video_run_store().list_runs(limit=limit)})


@router.get("/api/video-runs/{run_id}/pdf")
async def video_run_pdf(run_id: str):
    """Download the original PDF for a persistent run."""
    try:
        manifest = get_video_run_store().load_manifest(run_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Run not found")

    pdf_path = ((manifest.get("paths") or {}).get("pdf") or "").strip()
    if not pdf_path or not os.path.isfile(pdf_path):
        raise HTTPException(status_code=404, detail="Run PDF not found")
    filename = str(manifest.get("original_filename") or os.path.basename(pdf_path) or "source.pdf")
    return FileResponse(pdf_path, media_type="application/pdf", filename=filename)


@router.get("/api/video-runs/{run_id}/thumbnail")
async def video_run_thumbnail(
    run_id: str,
    page: int = Query(1, ge=1),
):
    """Render/cache one page image from a persistent run PDF."""
    try:
        manifest = get_video_run_store().load_manifest(run_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Run not found")

    pdf_path = ((manifest.get("paths") or {}).get("pdf") or "").strip()
    if not pdf_path or not os.path.isfile(pdf_path):
        raise HTTPException(status_code=404, detail="Run PDF not found")

    pages_dir = get_video_run_store().run_dir(run_id) / "pages"
    page_dir = pages_dir / f"page_{page:03d}"
    cache_path = page_dir / f"page_{page:03d}.jpg"
    if cache_path.is_file():
        return FileResponse(str(cache_path), media_type="image/jpeg")
    legacy_cache_path = pages_dir / f"page_{page:03d}.jpg"
    if legacy_cache_path.is_file():
        return FileResponse(str(legacy_cache_path), media_type="image/jpeg")

    try:
        images = convert_from_path(
            pdf_path,
            first_page=page,
            last_page=page,
            thread_count=1,
            poppler_path=os.getenv("POPPLER_PATH", None),
        )
        if not images:
            raise HTTPException(status_code=500, detail="Failed to render PDF page")
        page_dir.mkdir(parents=True, exist_ok=True)
        img = images[0].convert("RGB")
        max_w = 1280
        if img.width > max_w:
            ratio = max_w / max(img.width, 1)
            img = img.resize((max_w, max(1, int(img.height * ratio))), Image.Resampling.LANCZOS)
        img.save(cache_path, format="JPEG", quality=82, optimize=True)
        return FileResponse(str(cache_path), media_type="image/jpeg")
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"[VideoRun] thumbnail failed: {exc}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Run thumbnail failed: {exc}")


@router.get("/api/video-runs/{run_id}/pages/{page_index}/image")
async def get_video_run_page_image(run_id: str, page_index: int):
    """Return the persisted page image without rendering the PDF again."""
    try:
        manifest = get_video_run_store().load_manifest(run_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Run not found")

    pages = manifest.get("pages") or []
    if page_index < 0 or page_index >= len(pages):
        raise HTTPException(status_code=404, detail="Page not found")

    page = pages[page_index] or {}
    candidates = []
    slide_path = ((page.get("paths") or {}).get("slide") or "").strip()
    if slide_path:
        candidates.append(slide_path)
    pdir = get_video_run_store().page_dir(run_id, page_index)
    candidates.extend([
        str(pdir / f"page_{page_index + 1:03d}.jpg"),
        str(pdir / f"page_{page_index + 1:03d}.png"),
        str(get_video_run_store().run_dir(run_id) / "pages" / f"page_{page_index + 1:03d}.jpg"),
    ])

    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            ext = os.path.splitext(candidate)[1].lower()
            media_type = "image/png" if ext == ".png" else "image/jpeg"
            return FileResponse(candidate, media_type=media_type)
    raise HTTPException(status_code=404, detail="Page image not found")


@router.get("/api/video-runs/{run_id}")
async def get_video_run(run_id: str):
    """Return a persistent run manifest."""
    try:
        return JSONResponse(get_video_run_store().load_manifest(run_id))
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Run not found")


@router.patch("/api/video-runs/{run_id}")
async def update_video_run(run_id: str, req: VideoRunUpdateRequest):
    """Update editable run metadata, currently the sidebar display name."""
    try:
        if req.display_name is None:
            raise ValueError("display_name is required")
        return JSONResponse(get_video_run_store().rename_run(run_id, req.display_name))
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Run not found")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.patch("/api/video-runs/{run_id}/scripts")
async def update_video_run_scripts(run_id: str, req: VideoRunScriptsUpdateRequest):
    """Persist editable page scripts for a video run."""
    try:
        return JSONResponse(get_video_run_store().update_page_scripts(run_id, req.scripts))
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Run not found")


@router.post("/api/video-runs/{run_id}/scripts/generate")
async def generate_video_run_scripts(run_id: str, req: VideoRunGenerateScriptsRequest):
    """Generate scripts from run PDF text; for single-page generation, select by page index."""
    try:
        manifest = get_video_run_store().load_manifest(run_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Run not found")

    pdf_path = ((manifest.get("paths") or {}).get("pdf") or "").strip()
    if not pdf_path or not os.path.isfile(pdf_path):
        raise HTTPException(status_code=404, detail="Run PDF not found")

    from backend.app.services.utility.api import get_llm_api_key, llm_is_configured
    api_key = get_llm_api_key()
    if not llm_is_configured():
        raise HTTPException(status_code=400, detail="LLM provider, model or endpoint is not configured in backend/.env")

    pages = manifest.get("pages") or []
    page_count = len(pages)
    scope = str(req.scope or "all").strip().lower()
    if scope == "current":
        requested = req.pages if isinstance(req.pages, list) and req.pages else []
    else:
        requested = list(range(page_count))
    requested = sorted({int(i) for i in requested if 0 <= int(i) < page_count})
    if not requested:
        raise HTTPException(status_code=400, detail="No valid pages requested")

    try:
        from backend.app.services.utility.api import (
            generate_presentation_scripts,
            generate_presentation_scripts_from_pdf_file,
            get_configured_llm_provider,
            get_llm_model_name,
        )
        provider = get_configured_llm_provider(api_key)
        model_name = get_llm_model_name(provider)
        logger.info(
            "[VideoRun] scripts.generate run=%s scope=%s pages=%s provider=%s model=%s key_prefix=%s",
            run_id,
            scope,
            requested,
            provider,
            model_name,
            (api_key[:8] + "***") if api_key else "",
        )

        gen_requested: list[int] = []
        generated_map: dict[int, str] = {}

        # Gemini accepts the original PDF directly. Other built-in and custom
        # OpenAI-compatible providers use the PDF text extraction path so a
        # local endpoint does not need multimodal/PDF support.
        if provider != "google":
            from backend.app.services.utility.pdf import pdf_to_text_array

            page_texts = list(await asyncio.to_thread(pdf_to_text_array, pdf_path) or [])
            selected_texts = [page_texts[idx] if idx < len(page_texts) else "" for idx in requested]
            generated = await generate_presentation_scripts(
                text_array=selected_texts,
                api_key=api_key,
                language=req.language,
            )
            for result_idx, page_idx in enumerate(requested):
                body = generated[result_idx] if result_idx < len(generated or []) else ""
                normalized = _trim_redundant_opening(page_idx, _normalize_script_text(body))
                if normalized:
                    generated_map[page_idx] = normalized
            gen_requested = [idx for idx in requested if generated_map.get(idx)]

            scripts = [str(p.get("script") or "") for p in pages]
            while len(scripts) < page_count:
                scripts.append("")
            if scope == "all":
                for page_idx in requested:
                    scripts[page_idx] = ""
            for page_idx in gen_requested:
                scripts[page_idx] = generated_map[page_idx]
            updated_manifest = get_video_run_store().update_page_scripts(run_id, scripts)
            return JSONResponse({
                "run": updated_manifest,
                "scripts": scripts,
                "updated_pages": gen_requested,
                "skipped_empty_pages": [idx for idx in requested if idx not in set(gen_requested)],
                "text_stats": {
                    "requested_pages": len(requested),
                    "non_empty_pages": len(gen_requested),
                },
                "source": "pdf-text-extraction",
                "provider": provider,
                "scope": scope,
            })

        if scope == "all":
            lang = str(req.language or "zh").lower()
            if lang.startswith("en"):
                script_prompt = (
                    "Rewrite the whole deck into per-page spoken scripts in one output. "
                    "STRICT FORMAT REQUIRED: for page N, output exactly:\n"
                    "#PAGE_NNN#\n"
                    "<one complete paragraph>\n"
                    "#END_PAGE_NNN#\n"
                    "where NNN is 3-digit page number (001, 002...). "
                    "Never skip any requested page tag. "
                    "Page 1 can have one short opening. Page 2+ must continue without repeated greetings. "
                    "Adjust script length by slide information density instead of forcing equal length. "
                    "Title, outline, section divider, or transition slides should stay concise. "
                    "Slides with methods, diagrams, experimental results, comparison data, or multiple key points should be more complete: explain trends, differences, implications, and why the viewer should care, rather than merely reading slide text. "
                    "The overall output should be fuller than a short summary and suitable for direct voice-over recording. "
                    "Keep proper nouns, paper titles, author names, technical terms in original form. "
                    "Each page must end with a complete sentence."
                )
            else:
                script_prompt = (
                    "請將整份簡報一次改寫成逐頁口語講稿。"
                    "必須嚴格使用以下標記格式輸出每頁：\n"
                    "#PAGE_NNN#\n"
                    "<單一完整段落>\n"
                    "#END_PAGE_NNN#\n"
                    "其中 NNN 為三位數頁碼（001、002...）。不得漏頁、不得改標記字串。"
                    "第1頁可簡短開場，第2頁起禁止重複開場白。"
                    "請依每頁資訊密度分配講稿長度，不要讓每頁長度完全一致。"
                    "若該頁是目錄、章節切換、單純標題或過渡頁，請簡短帶過。"
                    "若該頁包含方法流程、圖表、比較數據、實驗結果或多個重點，請提供更完整的口語說明，說明圖表/數據代表的趨勢、差異與意義，而不是只覆述投影片文字。"
                    "整體講稿應比簡短摘要更完整，適合直接用於語音簡報錄製。"
                    "專有名詞、人名、論文標題、技術術語請保留原文，不要硬翻。"
                    "每頁必須是完整句結尾，不可用冒號或未完成列點作結。"
                )
            # Force per-click variation to avoid near-identical outputs across repeated "fill all".
            script_prompt += f"\n本次生成識別碼（僅作去重，不可輸出於最終內容）：{uuid.uuid4().hex[:12]}"
            generated_all_text = await generate_presentation_scripts_from_pdf_file(
                pdf_path=pdf_path,
                prompt=script_prompt,
                api_key=api_key,
                model_name_override=model_name,
                temperature=0.9,
            )
            parsed = _parse_tagged_pages(str(generated_all_text or ""), requested)
            if not parsed:
                parsed = _split_bulk_script_to_pages(str(generated_all_text or ""), requested)
            for page_idx, body in parsed.items():
                generated_map[page_idx] = _trim_redundant_opening(page_idx, _normalize_script_text(body))
            gen_requested = [idx for idx in requested if generated_map.get(idx)]
        else:
            page_idx = requested[0]
            page_no = page_idx + 1
            lang = str(req.language or "zh").lower()
            if lang.startswith("en"):
                prompt = (
                    "Generate spoken script for exactly one slide from this PDF. "
                    f"Only output page {page_no} in this exact format:\n"
                    f"#PAGE_{page_no:03d}#\n"
                    "<one complete paragraph>\n"
                    f"#END_PAGE_{page_no:03d}#\n"
                    "Adjust length by this slide's information density. "
                    "If it is a title, outline, section divider, or transition slide, keep it concise. "
                    "If it contains methods, diagrams, experimental results, comparison data, or multiple key points, give a more complete spoken explanation including trends, differences, implications, and why they matter. "
                    "Keep proper nouns and technical terms in original form. End with a complete sentence."
                )
            else:
                prompt = (
                    "請根據這份 PDF，只輸出指定單頁講稿。"
                    f"只可輸出第 {page_no} 頁，格式必須完全一致：\n"
                    f"#PAGE_{page_no:03d}#\n"
                    "<單一完整段落>\n"
                    f"#END_PAGE_{page_no:03d}#\n"
                    "請依此頁資訊密度決定講稿長度。若是標題、目錄、章節切換或過渡頁，請簡短帶過。"
                    "若包含方法流程、圖表、比較數據、實驗結果或多個重點，請提供較完整的口語說明，說明趨勢、差異與意義，不要只覆述投影片文字。"
                    "專有名詞、人名、術語請保留原文。"
                )
            one_text = await generate_presentation_scripts_from_pdf_file(
                pdf_path=pdf_path,
                prompt=prompt,
                api_key=api_key,
                model_name_override=model_name,
                temperature=0.75,
            )
            parsed_one = _parse_tagged_pages(str(one_text or ""), [page_idx])
            candidate_raw = parsed_one.get(page_idx, "")
            candidate = _trim_redundant_opening(page_idx, _normalize_script_text(candidate_raw))
            if candidate:
                generated_map[page_idx] = candidate
                gen_requested = [page_idx]

        scripts = [str(p.get("script") or "") for p in pages]
        while len(scripts) < page_count:
            scripts.append("")
        # Avoid stale old content on "fill all": requested pages are always rewritten
        # by this call. If a page is still missing after retries, keep it empty instead
        # of silently preserving old script.
        if scope == "all":
            for page_idx in requested:
                scripts[page_idx] = ""
        for page_idx in gen_requested:
            scripts[page_idx] = generated_map.get(page_idx, "")
        manifest = get_video_run_store().update_page_scripts(run_id, scripts)

        return JSONResponse({
            "run": manifest,
            "scripts": scripts,
            "updated_pages": gen_requested,
            "skipped_empty_pages": [idx for idx in requested if idx not in set(gen_requested)],
            "text_stats": {
                "requested_pages": len(requested),
                "non_empty_pages": len(gen_requested),
            },
            "source": "pdf-direct-file",
            "scope": scope,
        })
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"[VideoRun] script generation failed: {exc}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"LLM 講稿生成失敗: {exc}")


@router.patch("/api/video-runs/{run_id}/settings")
async def update_video_run_settings(
    run_id: str,
    settings_json: str = Form("{}"),
    reference_audio: Optional[UploadFile] = File(None),
):
    """Persist current editable voice/subtitle settings for a run."""
    try:
        import json
        try:
            settings = json.loads(settings_json or "{}")
        except Exception:
            raise ValueError("settings_json must be valid JSON")
        if not isinstance(settings, dict):
            raise ValueError("settings_json must be a JSON object")
        audio_bytes = await reference_audio.read() if reference_audio is not None else None
        audio_name = reference_audio.filename if reference_audio is not None else "reference.wav"
        return JSONResponse(get_video_run_store().update_settings(
            run_id,
            settings,
            reference_audio=audio_bytes,
            reference_audio_name=audio_name or "reference.wav",
        ))
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Run not found")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/api/video-runs/{run_id}/reference-audio")
async def get_video_run_reference_audio(run_id: str):
    """Return the custom reference voice audio saved with this run."""
    try:
        manifest = get_video_run_store().load_manifest(run_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Run not found")
    ref = ((manifest.get("settings") or {}).get("current") or {}).get("reference_audio") or {}
    path = str(ref.get("path") or "")
    if not path or not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="Reference audio not found")
    return FileResponse(path, media_type="audio/wav", filename=str(ref.get("filename") or "reference.wav"))


@router.delete("/api/video-runs/{run_id}")
async def delete_video_run(run_id: str):
    """Delete a persistent run and all artifacts under data/video_runs/<run_id>."""
    try:
        get_video_run_store().delete_run(run_id)
        return JSONResponse({"ok": True, "run_id": run_id})
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Run not found")


@router.get("/api/video-runs/{run_id}/pages/{page_index}/variants/{variant_id}/video")
async def get_video_run_variant_video(run_id: str, page_index: int, variant_id: str):
    """Return one persisted page-variant MP4."""
    try:
        path = get_video_run_store().get_variant_video_path(
            run_id=run_id,
            page_index=page_index,
            variant_id=variant_id,
        )
        return FileResponse(str(path), media_type="video/mp4", filename=f"{run_id}_page_{page_index + 1}_{variant_id}.mp4")
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Variant video not found")
    except IndexError:
        raise HTTPException(status_code=404, detail="Page not found")


@router.get("/api/video-runs/{run_id}/pages/{page_index}/variants/{variant_id}/audio")
async def get_video_run_variant_audio(run_id: str, page_index: int, variant_id: str):
    """Stream one persisted TTS result without routing it through browser uploads."""
    try:
        path = get_video_run_store().get_variant_audio_path(
            run_id=run_id, page_index=page_index, variant_id=variant_id,
        )
        return FileResponse(
            str(path), media_type="audio/wav",
            filename=f"{run_id}_page_{page_index + 1}_{variant_id}.wav",
        )
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Variant audio not found")
    except IndexError:
        raise HTTPException(status_code=404, detail="Page not found")


@router.get("/api/video-runs/{run_id}/pages/{page_index}/variants/{variant_id}/subtitles.srt")
async def get_video_run_variant_srt(run_id: str, page_index: int, variant_id: str):
    """Download the persisted sidecar subtitle timeline for a page variant."""
    try:
        path = get_video_run_store().get_variant_srt_path(
            run_id=run_id, page_index=page_index, variant_id=variant_id,
        )
        return FileResponse(
            str(path), media_type="application/x-subrip; charset=utf-8",
            filename=f"{run_id}_page_{page_index + 1}_{variant_id}.srt",
        )
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Variant SRT not found")
    except IndexError:
        raise HTTPException(status_code=404, detail="Page not found")


@router.get("/api/video-runs/{run_id}/pages/{page_index}/variants/{variant_id}/download.zip")
async def get_video_run_variant_bundle(run_id: str, page_index: int, variant_id: str):
    """Download a page video and its optional SRT as one ZIP archive."""
    store = get_video_run_store()
    try:
        video_path = str(store.get_variant_video_path(
            run_id=run_id, page_index=page_index, variant_id=variant_id,
        ))
        entries = [(video_path, f"page_{page_index + 1}.mp4")]
        try:
            srt_path = str(store.get_variant_srt_path(
                run_id=run_id, page_index=page_index, variant_id=variant_id,
            ))
            entries.append((srt_path, f"page_{page_index + 1}.srt"))
        except FileNotFoundError:
            pass
        return _download_bundle(entries, f"{run_id}_page_{page_index + 1}_{variant_id}.zip")
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Variant video not found")
    except IndexError:
        raise HTTPException(status_code=404, detail="Page not found")


@router.post("/api/video-runs/{run_id}/pages/{page_index}/variants/{variant_id}/select")
async def select_video_run_variant(run_id: str, page_index: int, variant_id: str):
    """Select one persisted variant as the main variant for merge/export."""
    try:
        page = get_video_run_store().select_page_variant(
            run_id=run_id,
            page_index=page_index,
            variant_id=variant_id,
        )
        return JSONResponse(page)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Variant not found")
    except IndexError:
        raise HTTPException(status_code=404, detail="Page not found")


@router.delete("/api/video-runs/{run_id}/pages/{page_index}/variants/{variant_id}")
async def delete_video_run_variant(run_id: str, page_index: int, variant_id: str):
    """Delete one persisted page variant."""
    try:
        page = get_video_run_store().delete_page_variant(
            run_id=run_id,
            page_index=page_index,
            variant_id=variant_id,
        )
        return JSONResponse(page)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Variant not found")
    except IndexError:
        raise HTTPException(status_code=404, detail="Page not found")


@router.post("/api/video-runs/{run_id}/exports/merge-selected")
async def merge_selected_video_run_variants(
    run_id: str,
    page_indexes_json: str = Form("[]"),
    variant_ids_json: str = Form("{}"),
    response_mode: str = Form("json"),
    transitions_enabled: bool = Form(False),
):
    """Merge persisted page-variant MP4s and save the merged export variant.

    The frontend sends the currently rendered page indexes and a page-index to
    variant-id map.  If a page has no explicit entry, use the page's selected
    variant from the manifest.
    """
    store = get_video_run_store()
    try:
        manifest = store.load_manifest(run_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Run not found")
    download_filename = _safe_download_filename(
        manifest.get("display_name") or manifest.get("original_filename") or run_id,
        fallback=run_id,
    )

    try:
        page_indexes_raw = json.loads(page_indexes_json or "[]")
        variant_ids_raw = json.loads(variant_ids_json or "{}")
    except Exception:
        raise HTTPException(status_code=400, detail="合併參數格式錯誤")

    pages = manifest.get("pages") or []
    page_indexes: list[int] = []
    if isinstance(page_indexes_raw, list):
        for value in page_indexes_raw:
            try:
                idx = int(value)
            except Exception:
                continue
            if 0 <= idx < len(pages) and idx not in page_indexes:
                page_indexes.append(idx)
    if not page_indexes:
        page_indexes = [i for i, page in enumerate(pages) if page.get("selected_variant_id")]

    variant_ids = variant_ids_raw if isinstance(variant_ids_raw, dict) else {}
    wants_video_response = str(response_mode or "").strip().lower() in {"video", "blob", "download", "mp4"}
    input_paths: list[str] = []
    source_pages: list[int] = []
    source_variants: dict[str, str] = {}
    source_segment_paths: list[str] = []
    missing: list[str] = []

    for idx in page_indexes:
        page = pages[idx] if 0 <= idx < len(pages) else {}
        explicit = variant_ids.get(str(idx), variant_ids.get(idx, ""))
        variant_id = str(explicit or page.get("selected_variant_id") or "").strip()
        if not variant_id:
            missing.append(f"第 {idx + 1} 頁未選擇影片變體")
            continue
        try:
            path = store.get_variant_video_path(run_id=run_id, page_index=idx, variant_id=variant_id)
        except FileNotFoundError:
            missing.append(f"第 {idx + 1} 頁找不到已渲染影片 ({variant_id})")
            continue
        input_paths.append(str(path))
        source_pages.append(idx)
        source_variants[str(idx)] = variant_id
        variant = next((v for v in (page.get("variants") or []) if v.get("variant_id") == variant_id), {})
        source_segment_paths.append(str((variant.get("paths") or {}).get("segments") or ""))

    if not input_paths:
        detail = "沒有可合併的已渲染影片"
        if missing:
            detail += "：" + "；".join(missing[:5])
        raise HTTPException(status_code=400, detail=detail)

    exports = manifest.setdefault("exports", {})
    for old in exports.get("variants") or []:
        old_settings = old.get("settings") or {}
        old_video = (old.get("paths") or {}).get("video") or ""
        if (
            old.get("source_pages") == source_pages
            and old_settings.get("source_variants") == source_variants
            and bool((old_settings.get("transitions") or {}).get("enabled")) == bool(transitions_enabled)
            and old_video
            and os.path.isfile(old_video)
        ):
            store.select_export_variant(run_id=run_id, variant_id=old.get("variant_id"))
            if wants_video_response:
                return FileResponse(
                    old_video,
                    media_type="video/mp4",
                    headers={
                        "Content-Disposition": _content_disposition_attachment(download_filename),
                        "X-Export-Variant-Id": old.get("variant_id", ""),
                        "X-Export-Reused": "true",
                    },
                )
            return JSONResponse(
                {
                    "ok": True,
                    "reused": True,
                    "export_variant_id": old.get("variant_id"),
                    "variant": old,
                },
                headers={"X-Export-Variant-Id": old.get("variant_id", "")},
            )

    merge_temp_dir = tempfile.mkdtemp(prefix="slideai_run_merge_")
    try:
        merged_path, transition_metadata = await merge_video_files(
            input_paths,
            merge_temp_dir,
            transitions_enabled=transitions_enabled,
        )
    except (RuntimeError, ValueError) as exc:
        import shutil
        shutil.rmtree(merge_temp_dir, ignore_errors=True)
        raise HTTPException(status_code=500, detail=str(exc))
    merged_segments: list[dict] = []
    offset = 0.0
    for video_path, segment_path in zip(input_paths, source_segment_paths):
        if segment_path and os.path.isfile(segment_path):
            try:
                payload = json.loads(open(segment_path, encoding="utf-8").read())
                for segment in payload.get("segments") or []:
                    item = dict(segment)
                    item["start"] = float(item.get("start") or 0) + offset
                    item["end"] = float(item.get("end") or 0) + offset
                    item["words"] = [
                        {**dict(word), "start": float(word.get("start") or 0) + offset, "end": float(word.get("end") or 0) + offset}
                        for word in (item.get("words") or [])
                    ]
                    merged_segments.append(item)
            except Exception as exc:
                logger.warning("Cannot include page SRT in merged export: %s", exc)
        offset += await _media_duration_seconds(video_path)
    merged_srt = build_srt(merged_segments) if merged_segments else ""
    try:
        variant = store.record_export_variant(
            run_id=run_id,
            video_source_path=merged_path,
            source_pages=source_pages,
            settings={
                "source_variants": source_variants,
                "requested_page_indexes": page_indexes,
                "missing": missing,
                "transitions": transition_metadata,
            },
            label=f"merged-{len(source_pages)}-pages" + ("-transitions" if transitions_enabled else ""),
            srt_content=merged_srt,
        )
    finally:
        import shutil
        shutil.rmtree(merge_temp_dir, ignore_errors=True)

    if wants_video_response:
        return FileResponse(
            str((variant.get("paths") or {}).get("video") or ""),
            media_type="video/mp4",
            headers={
                "Content-Disposition": _content_disposition_attachment(download_filename),
                "X-Export-Variant-Id": variant.get("variant_id", ""),
                "X-Export-Reused": "false",
            },
        )

    return JSONResponse(
        {
            "ok": True,
            "reused": False,
            "export_variant_id": variant.get("variant_id", ""),
            "variant": variant,
        },
        headers={"X-Export-Variant-Id": variant.get("variant_id", "")},
    )


@router.get("/api/video-runs/{run_id}/exports/{variant_id}/video")
async def get_video_run_export_video(run_id: str, variant_id: str):
    """Return one persisted merged/export MP4."""
    try:
        store = get_video_run_store()
        manifest = store.load_manifest(run_id)
        path = store.get_export_video_path(run_id=run_id, variant_id=variant_id)
        filename = _safe_download_filename(
            manifest.get("display_name") or manifest.get("original_filename") or run_id,
            fallback=run_id,
        )
        return FileResponse(str(path), media_type="video/mp4", filename=filename)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Export video not found")


@router.get("/api/video-runs/{run_id}/exports/{variant_id}/subtitles.srt")
async def get_video_run_export_srt(run_id: str, variant_id: str):
    try:
        path = get_video_run_store().get_export_srt_path(run_id=run_id, variant_id=variant_id)
        return FileResponse(str(path), media_type="application/x-subrip; charset=utf-8", filename=f"{run_id}_{variant_id}.srt")
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Export SRT not found")


@router.get("/api/video-runs/{run_id}/exports/{variant_id}/download.zip")
async def get_video_run_export_bundle(run_id: str, variant_id: str):
    """Download a merged video and its optional SRT as one ZIP archive."""
    store = get_video_run_store()
    try:
        manifest = store.load_manifest(run_id)
        video_path = str(store.get_export_video_path(run_id=run_id, variant_id=variant_id))
        base_name = _safe_download_filename(
            manifest.get("display_name") or manifest.get("original_filename") or run_id,
            fallback=run_id,
        )
        base_name = re.sub(r"\.mp4$", "", base_name, flags=re.IGNORECASE)
        entries = [(video_path, f"{base_name}.mp4")]
        try:
            srt_path = str(store.get_export_srt_path(run_id=run_id, variant_id=variant_id))
            entries.append((srt_path, f"{base_name}.srt"))
        except FileNotFoundError:
            pass
        return _download_bundle(entries, f"{base_name}.zip")
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Export video not found")


@router.post("/api/video-runs/{run_id}/exports/{variant_id}/select")
async def select_video_run_export(run_id: str, variant_id: str):
    """Select one persisted merged/export variant."""
    try:
        return JSONResponse(get_video_run_store().select_export_variant(run_id=run_id, variant_id=variant_id))
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Export variant not found")


@router.delete("/api/video-runs/{run_id}/exports/{variant_id}")
async def delete_video_run_export(run_id: str, variant_id: str):
    """Delete one persisted merged/export variant."""
    try:
        return JSONResponse(get_video_run_store().delete_export_variant(run_id=run_id, variant_id=variant_id))
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Export variant not found")


@router.post("/api/video-runs/local-pdf")
async def create_video_run_from_local_pdf(req: LocalPdfRunRequest, request: Request):
    """Create a persistent run record from a server-local PDF path.

    This endpoint is for backend-only batch preparation. It does not require
    opening the web UI and does not render pages yet.
    """
    client_host = (request.client.host if request.client else "") or ""
    allow_remote = is_truthy_env("SLIDEAI_ALLOW_LOCAL_PDF_API", "false")
    if not allow_remote and client_host not in {"127.0.0.1", "::1", "localhost"}:
        raise HTTPException(status_code=403, detail="local-pdf endpoint is restricted to localhost")

    pdf_path = os.path.abspath(os.path.expanduser(req.pdf_path))
    if not os.path.isfile(pdf_path):
        raise HTTPException(status_code=404, detail=f"PDF not found: {pdf_path}")

    try:
        with open(pdf_path, "rb") as pdf_file:
            page_count = len(PyPDF2.PdfReader(pdf_file).pages)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"PDF read failed: {exc}")

    scripts = list(req.scripts or [])
    if not scripts:
        scripts = ["" for _ in range(page_count)]
    elif len(scripts) < page_count:
        scripts.extend(["" for _ in range(page_count - len(scripts))])
    elif len(scripts) > page_count:
        scripts = scripts[:page_count]

    try:
        manifest = get_video_run_store().create_run(
            pdf_path=pdf_path,
            original_filename=os.path.basename(pdf_path),
            scripts=scripts,
            settings={
                **(req.settings or {}),
                "batch_request": {
                    "subtitle_source": req.subtitle_source,
                    "run_label": req.run_label,
                },
            },
            source="api-local-pdf",
        )
        return JSONResponse(manifest)
    except Exception as exc:
        logger.error(f"[VideoRun] create from local PDF failed: {exc}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Create run failed: {exc}")
