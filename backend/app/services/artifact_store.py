from __future__ import annotations

import json
import os
import shutil
import uuid
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _safe_name(value: str, fallback: str = "untitled") -> str:
    raw = str(value or fallback).strip() or fallback
    keep = []
    for ch in raw:
        if ch.isalnum() or ch in {"-", "_", "."}:
            keep.append(ch)
        elif ch.isspace():
            keep.append("_")
    name = "".join(keep).strip("._")
    return (name or fallback)[:96]


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def get_video_runs_root() -> Path:
    configured = os.getenv("SLIDEAI_VIDEO_RUNS_DIR", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return (_repo_root() / "data" / "video_runs").resolve()


class VideoRunStore:
    """Filesystem-backed artifact store for PDF-to-video runs.

    This is intentionally simple: one directory per run, JSON manifest files,
    and page/variant subfolders. It gives us persistent inspection without
    introducing database coupling before the workflow stabilizes.
    """

    def __init__(self, root: Path | None = None) -> None:
        self.root = (root or get_video_runs_root()).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def new_run_id(self) -> str:
        return f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"

    def safe_filename(self, value: str, fallback: str = "source.pdf") -> str:
        return _safe_name(value, fallback)

    def run_dir(self, run_id: str) -> Path:
        return self.root / _safe_name(run_id, "run")

    def manifest_path(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "manifest.json"

    def create_run(
        self,
        *,
        pdf_path: str | Path,
        original_filename: str = "source.pdf",
        scripts: Optional[Iterable[str]] = None,
        settings: Optional[Dict[str, Any]] = None,
        pdf_id: Optional[str] = None,
        project_id: Optional[int] = None,
        source: str = "web",
        run_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        src = Path(pdf_path).expanduser().resolve()
        if not src.is_file():
            raise FileNotFoundError(f"PDF not found: {src}")
        run_id = run_id or self.new_run_id()
        rdir = self.run_dir(run_id)
        if (rdir / "manifest.json").exists():
            raise FileExistsError(f"run already exists: {run_id}")
        input_dir = rdir / "input"
        pages_dir = rdir / "pages"
        input_dir.mkdir(parents=True, exist_ok=True)
        pages_dir.mkdir(parents=True, exist_ok=True)

        pdf_name = _safe_name(original_filename or src.name, "source.pdf")
        if not pdf_name.lower().endswith(".pdf"):
            pdf_name += ".pdf"
        stored_pdf = input_dir / pdf_name
        if src != stored_pdf.resolve():
            shutil.copy2(src, stored_pdf)

        script_list = list(scripts or [])
        manifest: Dict[str, Any] = {
            "run_id": run_id,
            "status": "created",
            "source": source,
            "created_at": _utc_now(),
            "updated_at": _utc_now(),
            "pdf_id": pdf_id,
            "project_id": project_id,
            "original_filename": original_filename or src.name,
            "display_name": original_filename or src.name,
            "paths": {
                "root": str(rdir),
                "pdf": str(stored_pdf),
                "pages": str(pages_dir),
            },
            "settings": settings or {},
            "pages": [
                {
                    "page_index": i,
                    "page_number": i + 1,
                    "script": text,
                    "variants": [],
                    "selected_variant_id": None,
                }
                for i, text in enumerate(script_list)
            ],
        }
        self._write_json(self.manifest_path(run_id), manifest)
        self._write_json(rdir / "scripts.json", {"scripts": script_list})
        return manifest

    def load_manifest(self, run_id: str) -> Dict[str, Any]:
        path = self.manifest_path(run_id)
        if not path.is_file():
            raise FileNotFoundError(f"run not found: {run_id}")
        return json.loads(path.read_text(encoding="utf-8"))

    def save_manifest(self, manifest: Dict[str, Any]) -> None:
        manifest["updated_at"] = _utc_now()
        self._write_json(self.manifest_path(str(manifest["run_id"])), manifest)

    def list_runs(self, limit: int = 50) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for path in sorted(self.root.glob("*/manifest.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                out.append(self._summary(data))
            except Exception:
                continue
            if len(out) >= limit:
                break
        return out

    def delete_run(self, run_id: str) -> None:
        rdir = self.run_dir(run_id)
        if not rdir.is_dir() or not (rdir / "manifest.json").is_file():
            raise FileNotFoundError(f"run not found: {run_id}")
        shutil.rmtree(rdir)

    def rename_run(self, run_id: str, display_name: str) -> Dict[str, Any]:
        manifest = self.load_manifest(run_id)
        name = str(display_name or "").strip()
        if not name:
            raise ValueError("display_name is required")
        manifest["display_name"] = name[:120]
        self.save_manifest(manifest)
        return manifest

    def update_page_scripts(self, run_id: str, scripts: Iterable[str]) -> Dict[str, Any]:
        """Persist the editable page scripts in the run manifest.

        The web editor treats manifest.pages[].script as the source of truth
        when reopening a project, so edits must be written here instead of only
        living in Vue state.
        """
        manifest = self.load_manifest(run_id)
        script_list = [str(s or "") for s in (scripts or [])]
        pages = manifest.setdefault("pages", [])
        while len(pages) < len(script_list):
            i = len(pages)
            pages.append({
                "page_index": i,
                "page_number": i + 1,
                "script": "",
                "variants": [],
                "selected_variant_id": None,
            })
        for i, text in enumerate(script_list):
            pages[i]["script"] = text
        self.save_manifest(manifest)
        self._write_json(self.run_dir(run_id) / "scripts.json", {"scripts": [p.get("script", "") for p in pages]})
        return manifest

    def update_settings(
        self,
        run_id: str,
        settings: Dict[str, Any],
        reference_audio: bytes | None = None,
        reference_audio_name: str = "reference.wav",
    ) -> Dict[str, Any]:
        """Persist current editable UI settings for reopening/API reuse."""
        manifest = self.load_manifest(run_id)
        merged = dict(manifest.get("settings") or {})
        current = dict(merged.get("current") or {})
        current.update(settings or {})

        if reference_audio is not None:
            input_dir = self.run_dir(run_id) / "input"
            input_dir.mkdir(parents=True, exist_ok=True)
            safe_name = _safe_name(reference_audio_name or "reference.wav", "reference.wav")
            if "." not in safe_name:
                safe_name += ".wav"
            ref_path = input_dir / f"reference_audio{Path(safe_name).suffix or '.wav'}"
            ref_path.write_bytes(reference_audio)
            current["reference_audio"] = {
                "path": str(ref_path),
                "filename": safe_name,
            }
        elif current.get("selected_voice_key") == "custom" and current.get("has_reference_audio") is False:
            old_ref = current.pop("reference_audio", {}) or {}
            old_path = Path(str(old_ref.get("path") or ""))
            try:
                if old_path.is_file() and self.run_dir(run_id).resolve() in old_path.resolve().parents:
                    old_path.unlink()
            except Exception:
                pass

        merged["current"] = current
        manifest["settings"] = merged
        self.save_manifest(manifest)
        return manifest

    def page_dir(self, run_id: str, page_index: int) -> Path:
        return self.run_dir(run_id) / "pages" / f"page_{page_index + 1:03d}"

    def record_page_asset(
        self,
        *,
        run_id: str,
        page_index: int,
        slide_bytes: bytes | None = None,
        suffix: str = ".jpg",
    ) -> Dict[str, Any]:
        manifest = self.load_manifest(run_id)
        pdir = self.page_dir(run_id, page_index)
        pdir.mkdir(parents=True, exist_ok=True)
        page = self._ensure_page(manifest, page_index)
        if slide_bytes is not None:
            ext = suffix if suffix.startswith(".") else f".{suffix}"
            if ext.lower() not in {".jpg", ".jpeg", ".png"}:
                ext = ".jpg"
            slide_path = pdir / f"page_{page_index + 1:03d}{ext}"
            slide_path.write_bytes(slide_bytes)
            page.setdefault("paths", {})["slide"] = str(slide_path)
        else:
            jpg_path = pdir / f"page_{page_index + 1:03d}.jpg"
            png_path = pdir / f"page_{page_index + 1:03d}.png"
            if jpg_path.is_file():
                page.setdefault("paths", {})["slide"] = str(jpg_path)
            elif png_path.is_file():
                page.setdefault("paths", {})["slide"] = str(png_path)
        self.save_manifest(manifest)
        return page

    def record_page_tts(
        self,
        *,
        run_id: str,
        page_index: int,
        audio_bytes: bytes,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        manifest = self.load_manifest(run_id)
        page = self._ensure_page(manifest, page_index)
        digest = hashlib.sha1(audio_bytes + json.dumps(metadata or {}, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:10]
        tts_id = f"tts-{len(page.get('tts', [])) + 1:03d}-{digest}"
        tdir = self.page_dir(run_id, page_index) / "tts" / tts_id
        tdir.mkdir(parents=True, exist_ok=True)
        audio_path = tdir / "audio.wav"
        audio_path.write_bytes(audio_bytes)
        item = {
            "tts_id": tts_id,
            "created_at": _utc_now(),
            "status": "ready",
            "paths": {"audio": str(audio_path)},
            "metadata": metadata or {},
        }
        self._write_json(tdir / "tts.json", item)
        page.setdefault("tts", []).append(item)
        page["selected_tts_id"] = tts_id
        manifest["status"] = "rendering"
        self.save_manifest(manifest)
        return item

    def record_page_alignment(
        self,
        *,
        run_id: str,
        page_index: int,
        segments: List[Dict[str, Any]],
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        manifest = self.load_manifest(run_id)
        page = self._ensure_page(manifest, page_index)
        digest = hashlib.sha1(json.dumps(segments or [], sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:10]
        align_id = f"align-{len(page.get('alignments', [])) + 1:03d}-{digest}"
        adir = self.page_dir(run_id, page_index) / "align" / align_id
        adir.mkdir(parents=True, exist_ok=True)
        seg_path = adir / "segments.json"
        self._write_json(seg_path, {"segments": segments or []})
        item = {
            "align_id": align_id,
            "created_at": _utc_now(),
            "status": "ready",
            "paths": {"segments": str(seg_path)},
            "metadata": metadata or {},
        }
        self._write_json(adir / "alignment.json", item)
        page.setdefault("alignments", []).append(item)
        page["selected_align_id"] = align_id
        manifest["status"] = "rendering"
        self.save_manifest(manifest)
        return item

    def _ensure_page(self, manifest: Dict[str, Any], page_index: int) -> Dict[str, Any]:
        while len(manifest.get("pages", [])) <= page_index:
            i = len(manifest["pages"])
            manifest["pages"].append({
                "page_index": i,
                "page_number": i + 1,
                "script": "",
                "variants": [],
                "selected_variant_id": None,
            })
        return manifest["pages"][page_index]

    def _variant_dir(self, run_id: str, page_index: int, variant_id: str) -> Path:
        return self.page_dir(run_id, page_index) / "variants" / _safe_name(variant_id, "variant")

    def _find_variant(self, page: Dict[str, Any], variant_id: str) -> Optional[Dict[str, Any]]:
        for variant in page.get("variants") or []:
            if variant.get("variant_id") == variant_id:
                return variant
        return None

    def _create_page_variant_record(
        self,
        page: Dict[str, Any],
        *,
        label: str = "",
        settings: Optional[Dict[str, Any]] = None,
        status: str = "created",
    ) -> Dict[str, Any]:
        variant_id = f"v{len(page.get('variants', [])) + 1:03d}-{uuid.uuid4().hex[:6]}"
        variant = {
            "variant_id": variant_id,
            "label": label,
            "created_at": _utc_now(),
            "updated_at": _utc_now(),
            "status": status,
            "paths": {},
            "settings": settings or {},
        }
        page.setdefault("variants", []).append(variant)
        page["selected_variant_id"] = variant_id
        return variant

    def record_page_variant_tts(
        self,
        *,
        run_id: str,
        page_index: int,
        audio_bytes: bytes,
        metadata: Optional[Dict[str, Any]] = None,
        label: str = "",
    ) -> Dict[str, Any]:
        manifest = self.load_manifest(run_id)
        page = self._ensure_page(manifest, page_index)
        variant = self._create_page_variant_record(
            page,
            label=label or f"web-page-{page_index + 1}",
            settings={"tts": metadata or {}},
            status="tts_ready",
        )
        vdir = self._variant_dir(run_id, page_index, variant["variant_id"])
        vdir.mkdir(parents=True, exist_ok=True)
        audio_path = vdir / "audio.wav"
        audio_path.write_bytes(audio_bytes)
        tts_json = {
            "variant_id": variant["variant_id"],
            "created_at": _utc_now(),
            "status": "ready",
            "metadata": metadata or {},
            "paths": {"audio": str(audio_path)},
        }
        self._write_json(vdir / "tts.json", tts_json)
        variant.setdefault("paths", {})["audio"] = str(audio_path)
        variant["tts"] = tts_json
        variant["updated_at"] = _utc_now()
        manifest["status"] = "rendering"
        self.save_manifest(manifest)
        self._write_json(vdir / "variant.json", variant)
        return variant

    def record_page_variant_alignment(
        self,
        *,
        run_id: str,
        page_index: int,
        variant_id: str,
        segments: List[Dict[str, Any]],
        srt: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        manifest = self.load_manifest(run_id)
        page = self._ensure_page(manifest, page_index)
        variant = self._find_variant(page, variant_id)
        if variant is None:
            raise FileNotFoundError(f"variant not found: {variant_id}")
        vdir = self._variant_dir(run_id, page_index, variant_id)
        vdir.mkdir(parents=True, exist_ok=True)
        seg_path = vdir / "segments.json"
        self._write_json(seg_path, {"segments": segments or []})
        srt_path = vdir / "subtitles.srt"
        srt_path.write_text(srt or "", encoding="utf-8")
        align_json = {
            "variant_id": variant_id,
            "created_at": _utc_now(),
            "status": "ready",
            "metadata": metadata or {},
            "paths": {"segments": str(seg_path), "srt": str(srt_path)},
        }
        self._write_json(vdir / "alignment.json", align_json)
        variant.setdefault("paths", {})["segments"] = str(seg_path)
        variant.setdefault("paths", {})["srt"] = str(srt_path)
        variant["alignment"] = align_json
        variant["status"] = "align_ready"
        variant["updated_at"] = _utc_now()
        manifest["status"] = "rendering"
        self.save_manifest(manifest)
        self._write_json(vdir / "variant.json", variant)
        return variant

    def record_page_variant(
        self,
        *,
        run_id: str,
        page_index: int,
        video_bytes: bytes,
        audio_bytes: bytes | None = None,
        slide_bytes: bytes | None = None,
        segments: Optional[List[Dict[str, Any]]] = None,
        ass_content: str | None = None,
        settings: Optional[Dict[str, Any]] = None,
        label: str = "",
        variant_id: str = "",
    ) -> Dict[str, Any]:
        manifest = self.load_manifest(run_id)
        page = self._ensure_page(manifest, page_index)

        variant = self._find_variant(page, variant_id) if variant_id else None
        if variant is None:
            variant = self._create_page_variant_record(
                page,
                label=label,
                settings=settings or {},
                status="rendering",
            )
            variant_id = variant["variant_id"]
        vdir = self._variant_dir(run_id, page_index, variant_id)
        vdir.mkdir(parents=True, exist_ok=True)

        paths: Dict[str, str] = dict(variant.get("paths") or {})
        video_path = vdir / "video.mp4"
        video_path.write_bytes(video_bytes)
        paths["video"] = str(video_path)
        if audio_bytes is not None:
            audio_path = vdir / "audio.wav"
            audio_path.write_bytes(audio_bytes)
            paths["audio"] = str(audio_path)
        if slide_bytes is not None:
            page_paths = page.setdefault("paths", {})
            existing_slide = str(page_paths.get("slide") or "")
            if existing_slide and Path(existing_slide).is_file():
                slide_path = Path(existing_slide)
            else:
                # Keep one canonical page background image.  The run thumbnail
                # cache writes JPEG, so render artifacts should not create a
                # duplicate PNG beside it.
                slide_path = self.page_dir(run_id, page_index) / f"page_{page_index + 1:03d}.jpg"
                slide_path.write_bytes(slide_bytes)
            page.setdefault("paths", {})["slide"] = str(slide_path)
            paths["slide"] = str(slide_path)
        if ass_content is not None:
            ass_path = vdir / "subtitles.ass"
            ass_path.write_text(ass_content, encoding="utf-8")
            paths["ass"] = str(ass_path)
        if segments is not None:
            seg_path = vdir / "segments.json"
            self._write_json(seg_path, {"segments": segments})
            paths["segments"] = str(seg_path)

        variant["label"] = label or variant.get("label", "")
        variant["status"] = "rendered"
        variant["paths"] = paths
        variant["settings"] = settings or variant.get("settings") or {}
        variant["updated_at"] = _utc_now()
        page["selected_variant_id"] = variant_id
        manifest.setdefault("settings", {})["last_render"] = settings or {}
        manifest["status"] = "rendering" if any(not p.get("variants") for p in manifest["pages"]) else "rendered"
        self.save_manifest(manifest)
        self._write_json(vdir / "variant.json", variant)
        return variant

    def select_page_variant(self, *, run_id: str, page_index: int, variant_id: str) -> Dict[str, Any]:
        manifest = self.load_manifest(run_id)
        pages = manifest.get("pages") or []
        if page_index < 0 or page_index >= len(pages):
            raise IndexError("page index out of range")
        variants = pages[page_index].get("variants") or []
        if not any(v.get("variant_id") == variant_id for v in variants):
            raise FileNotFoundError(f"variant not found: {variant_id}")
        pages[page_index]["selected_variant_id"] = variant_id
        self.save_manifest(manifest)
        return pages[page_index]

    def delete_page_variant(self, *, run_id: str, page_index: int, variant_id: str) -> Dict[str, Any]:
        manifest = self.load_manifest(run_id)
        pages = manifest.get("pages") or []
        if page_index < 0 or page_index >= len(pages):
            raise IndexError("page index out of range")

        page = pages[page_index]
        variants = page.get("variants") or []
        kept = [v for v in variants if v.get("variant_id") != variant_id]
        if len(kept) == len(variants):
            raise FileNotFoundError(f"variant not found: {variant_id}")

        new_vdir = self.page_dir(run_id, page_index) / "variants" / _safe_name(variant_id, "variant")
        old_vdir = self.page_dir(run_id, page_index) / _safe_name(variant_id, "variant")
        shutil.rmtree(new_vdir, ignore_errors=True)
        shutil.rmtree(old_vdir, ignore_errors=True)
        page["variants"] = kept
        if page.get("selected_variant_id") == variant_id:
            page["selected_variant_id"] = kept[-1].get("variant_id") if kept else None
        manifest["status"] = "rendering" if any(not p.get("variants") for p in pages) else "rendered"
        self.save_manifest(manifest)
        return page

    def get_variant_video_path(self, *, run_id: str, page_index: int, variant_id: str) -> Path:
        manifest = self.load_manifest(run_id)
        pages = manifest.get("pages") or []
        if page_index < 0 or page_index >= len(pages):
            raise IndexError("page index out of range")
        for variant in pages[page_index].get("variants") or []:
            if variant.get("variant_id") == variant_id:
                video = ((variant.get("paths") or {}).get("video") or "").strip()
                if not video:
                    break
                path = Path(video)
                if path.is_file():
                    return path
                break
        raise FileNotFoundError(f"variant video not found: {variant_id}")

    def get_variant_srt_path(self, *, run_id: str, page_index: int, variant_id: str) -> Path:
        manifest = self.load_manifest(run_id)
        pages = manifest.get("pages") or []
        if page_index < 0 or page_index >= len(pages):
            raise IndexError("page index out of range")
        for variant in pages[page_index].get("variants") or []:
            if variant.get("variant_id") == variant_id:
                path = Path(str((variant.get("paths") or {}).get("srt") or ""))
                if path.is_file():
                    return path
                break
        raise FileNotFoundError(f"variant SRT not found: {variant_id}")

    def record_export_variant(
        self,
        *,
        run_id: str,
        video_bytes: bytes,
        source_pages: Optional[List[int]] = None,
        settings: Optional[Dict[str, Any]] = None,
        label: str = "",
        srt_content: str | None = None,
    ) -> Dict[str, Any]:
        manifest = self.load_manifest(run_id)
        exports = manifest.setdefault("exports", {})
        variants = exports.setdefault("variants", [])
        variant_id = f"export-v{len(variants) + 1:03d}-{uuid.uuid4().hex[:6]}"
        vdir = self.run_dir(run_id) / "exports" / variant_id
        vdir.mkdir(parents=True, exist_ok=True)

        video_path = vdir / "video.mp4"
        video_path.write_bytes(video_bytes)
        paths = {"video": str(video_path)}
        if srt_content:
            srt_path = vdir / "subtitles.srt"
            srt_path.write_text(srt_content, encoding="utf-8")
            paths["srt"] = str(srt_path)
        variant = {
            "variant_id": variant_id,
            "label": label,
            "created_at": _utc_now(),
            "status": "rendered",
            "source_pages": source_pages or [],
            "paths": paths,
            "settings": settings or {},
        }
        variants.append(variant)
        exports["selected_variant_id"] = variant_id
        self.save_manifest(manifest)
        self._write_json(vdir / "variant.json", variant)
        return variant

    def select_export_variant(self, *, run_id: str, variant_id: str) -> Dict[str, Any]:
        manifest = self.load_manifest(run_id)
        exports = manifest.setdefault("exports", {})
        variants = exports.get("variants") or []
        if not any(v.get("variant_id") == variant_id for v in variants):
            raise FileNotFoundError(f"export variant not found: {variant_id}")
        exports["selected_variant_id"] = variant_id
        self.save_manifest(manifest)
        return exports

    def delete_export_variant(self, *, run_id: str, variant_id: str) -> Dict[str, Any]:
        manifest = self.load_manifest(run_id)
        exports = manifest.setdefault("exports", {})
        variants = exports.get("variants") or []
        kept = [v for v in variants if v.get("variant_id") != variant_id]
        if len(kept) == len(variants):
            raise FileNotFoundError(f"export variant not found: {variant_id}")
        vdir = self.run_dir(run_id) / "exports" / _safe_name(variant_id, "variant")
        shutil.rmtree(vdir, ignore_errors=True)
        exports["variants"] = kept
        if exports.get("selected_variant_id") == variant_id:
            exports["selected_variant_id"] = kept[-1].get("variant_id") if kept else None
        self.save_manifest(manifest)
        return exports

    def get_export_video_path(self, *, run_id: str, variant_id: str) -> Path:
        manifest = self.load_manifest(run_id)
        exports = manifest.get("exports") or {}
        for variant in exports.get("variants") or []:
            if variant.get("variant_id") == variant_id:
                video = ((variant.get("paths") or {}).get("video") or "").strip()
                if video:
                    path = Path(video)
                    if path.is_file():
                        return path
                break
        raise FileNotFoundError(f"export video not found: {variant_id}")

    def get_export_srt_path(self, *, run_id: str, variant_id: str) -> Path:
        manifest = self.load_manifest(run_id)
        exports = manifest.get("exports") or {}
        for variant in exports.get("variants") or []:
            if variant.get("variant_id") == variant_id:
                path = Path(str((variant.get("paths") or {}).get("srt") or ""))
                if path.is_file():
                    return path
                break
        raise FileNotFoundError(f"export SRT not found: {variant_id}")

    def _summary(self, manifest: Dict[str, Any]) -> Dict[str, Any]:
        pages = manifest.get("pages") or []
        rendered = sum(1 for p in pages if p.get("variants"))
        return {
            "run_id": manifest.get("run_id"),
            "status": manifest.get("status"),
            "source": manifest.get("source"),
            "created_at": manifest.get("created_at"),
            "updated_at": manifest.get("updated_at"),
            "original_filename": manifest.get("original_filename"),
            "display_name": manifest.get("display_name") or manifest.get("original_filename"),
            "pdf_id": manifest.get("pdf_id"),
            "project_id": manifest.get("project_id"),
            "page_count": len(pages),
            "rendered_pages": rendered,
            "root": (manifest.get("paths") or {}).get("root"),
        }

    @staticmethod
    def _write_json(path: Path, data: Dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)


_STORE: VideoRunStore | None = None


def get_video_run_store() -> VideoRunStore:
    global _STORE
    if _STORE is None:
        _STORE = VideoRunStore()
    return _STORE
