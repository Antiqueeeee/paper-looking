"""Render original PDF pages to JPEG for browser-native preview."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


class PDFPreviewError(RuntimeError):
    pass


def _tool(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise PDFPreviewError(f"{name} not installed; install poppler-utils")
    return path


def render_pdf_pages(pdf_path: str | Path, out_dir: str | Path, dpi: int = 130) -> list[Path]:
    """Render every PDF page as JPEG into out_dir. Returns sorted page paths."""
    pdf_path = Path(pdf_path)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    prefix = out_dir / "page"
    cmd = [
        _tool("pdftoppm"),
        "-jpeg",
        "-jpegopt", "quality=85",
        "-r", str(dpi),
        str(pdf_path),
        str(prefix),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired as exc:
        raise PDFPreviewError("pdftoppm timed out") from exc
    if proc.returncode != 0:
        raise PDFPreviewError((proc.stderr or f"pdftoppm exit {proc.returncode}").strip()[:800])
    pages = sorted(out_dir.glob("page-*.jpg"))
    if not pages:
        raise PDFPreviewError("pdftoppm produced no pages")
    return pages


def ensure_preview_images(pdf_path: str | Path, preview_dir: str | Path, dpi: int = 130) -> list[Path]:
    """Return cached preview images, rendering them when missing."""
    pdf_path = Path(pdf_path)
    preview_dir = Path(preview_dir)
    pages = sorted(preview_dir.glob("page-*.jpg")) if preview_dir.exists() else []
    if not pages:
        pages = render_pdf_pages(pdf_path, preview_dir, dpi=dpi)
    return pages


__all__ = ["PDFPreviewError", "render_pdf_pages", "ensure_preview_images"]
