"""PDF download, validation, upload ingestion and cold/hot helpers."""
from __future__ import annotations

import hashlib
import logging
import re
import shutil
from pathlib import Path

import requests

from paperbase.db import get_paper, set_local_file, set_pdf_status, upsert_paper
from paperbase.paths import PaperPaths
from paperbase.storage import ensure_capacity, QuotaExceeded
from paperbase.tasks import content_hash, enqueue_task

logger = logging.getLogger(__name__)
PDF_MAGIC = b"%PDF-"


def file_sha256(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def validate_pdf(path: str | Path) -> tuple[bool, str]:
    p = Path(path)
    if not p.exists() or p.stat().st_size == 0:
        return False, "empty or missing file"
    with open(p, "rb") as f:
        if f.read(5) != PDF_MAGIC:
            return False, "not a PDF file (bad magic)"
    return True, ""


def normalize_title_for_match(title: str) -> str:
    return re.sub(r"[\W_]+", " ", title.lower()).strip()


def find_paper_by_title(conn, title: str) -> dict | None:
    needle = normalize_title_for_match(title)
    if not needle:
        return None
    rows = conn.execute(
        "SELECT * FROM papers WHERE title != '' ORDER BY year DESC LIMIT 5000"
    ).fetchall()
    for row in rows:
        if normalize_title_for_match(row["title"]) == needle:
            return dict(row)
    return None


def download_pdf(conn, paper: dict, paths: PaperPaths, config: dict, store=None) -> Path:
    """Download a paper's open PDF into the hot directory.

    Returns the local path. Raises on permanent failure after retries.
    """
    pdf_url = (paper.get("pdf_url") or "").strip()
    if not pdf_url.startswith("http"):
        raise RuntimeError("paper has no downloadable pdf_url")

    dest = paths.hot_pdf(paper["id"])
    if dest.exists():
        ok, _ = validate_pdf(dest)
        if ok:
            return dest
        dest.unlink(missing_ok=True)

    pdf_cfg = config.get("pdf", {})
    timeout = int(pdf_cfg.get("download_timeout_seconds", 120))
    retries = int(pdf_cfg.get("download_retries", 2))
    quota_bytes = int(float(pdf_cfg.get("hot_quota_gb", 6)) * 1024 ** 3)

    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            with requests.get(
                pdf_url,
                stream=True,
                timeout=timeout,
                headers={"User-Agent": "paperbase/0.1"},
            ) as resp:
                resp.raise_for_status()
                total = int(resp.headers.get("Content-Length") or 0)
                if total:
                    try:
                        ensure_capacity(paths.pdf_hot_dir, total, quota_bytes)
                    except QuotaExceeded:
                        if store is not None:
                            from paperbase.pipeline.storage_manager import evict_hot_pdfs

                            evict_hot_pdfs(conn, paths, store, need_free_bytes=total)
                        ensure_capacity(paths.pdf_hot_dir, total, quota_bytes)
                dest.parent.mkdir(parents=True, exist_ok=True)
                tmp = dest.with_suffix(".pdf.part")
                with open(tmp, "wb") as f:
                    for chunk in resp.iter_content(1024 * 256):
                        if chunk:
                            f.write(chunk)
                ok, reason = validate_pdf(tmp)
                if not ok:
                    tmp.unlink(missing_ok=True)
                    raise RuntimeError(f"downloaded invalid PDF: {reason}")
                shutil.move(str(tmp), dest)
            break
        except Exception as exc:  # retry transient/validation failures
            last_error = exc
            if attempt < retries:
                logger.warning("download retry %s: %s", pdf_url, exc)
                continue
    else:
        raise RuntimeError(f"download failed: {last_error}") from last_error

    set_local_file(conn, paper["id"], local_pdf=str(dest), pdf_sha256=file_sha256(dest))
    set_pdf_status(conn, paper["id"], "downloaded")
    return dest


def ingest_uploaded_pdf(
    conn,
    paths: PaperPaths,
    config: dict,
    file_path: str | Path,
    *,
    paper_id: str | None = None,
    title: str | None = None,
) -> dict:
    """Validate, deduplicate and ingest a manually uploaded PDF.

    Returns the canonical paper dict. Creates a manual record when no paper
    id is supplied and no title match is found.
    """
    src = Path(file_path)
    if not src.exists():
        raise FileNotFoundError(src)

    max_mb = int(config.get("pdf", {}).get("max_upload_mb", 100))
    size = src.stat().st_size
    if size > max_mb * 1024 * 1024:
        raise ValueError(f"PDF too large: {size} bytes > {max_mb}MB")
    ok, reason = validate_pdf(src)
    if not ok:
        raise ValueError(f"invalid PDF: {reason}")

    digest = file_sha256(src)
    paper = None
    if paper_id:
        paper = get_paper(conn, str(paper_id))
    if paper is None and title:
        paper = find_paper_by_title(conn, title)
    if paper is None:
        # Manual record with content-derived canonical id.
        paper_id = f"manual:{digest[:16]}"
        paper = get_paper(conn, paper_id)
        if paper is None:
            upsert_paper(conn, {
                "id": paper_id,
                "source": "manual",
                "title": title or Path(src).stem,
                "authors": [],
                "abstract": "",
                "year": None,
                "venue": "manual",
                "url": "",
                "pdf_url": "",
                "doi": "",
                "tags": [],
            })
            paper = get_paper(conn, paper_id)

    # Duplicate content returns the existing canonical record without copying.
    if paper.get("pdf_sha256") == digest:
        return paper

    dest = paths.hot_pdf(paper["id"])
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and file_sha256(dest) == digest:
        set_local_file(conn, paper["id"], local_pdf=str(dest), pdf_sha256=digest)
    else:
        quota_bytes = int(float(config.get("pdf", {}).get("hot_quota_gb", 6)) * 1024 ** 3)
        try:
            ensure_capacity(paths.pdf_hot_dir, size, quota_bytes)
        except QuotaExceeded:
            from paperbase.pipeline.storage_manager import evict_hot_pdfs, get_object_store

            store = get_object_store(config, paths)
            evict_hot_pdfs(conn, paths, store, need_free_bytes=size, quota_bytes=quota_bytes)
            ensure_capacity(paths.pdf_hot_dir, size, quota_bytes)
        shutil.copyfile(src, dest)
    set_local_file(conn, paper["id"], local_pdf=str(dest), pdf_sha256=digest)
    set_pdf_status(conn, paper["id"], "downloaded")
    enqueue_task(
        conn,
        paper_id=paper["id"],
        task_type="parse_pdf",
        payload={"pdf_path": str(dest), "sha256": digest},
        input_hash=content_hash("parse_pdf", digest),
    )
    return get_paper(conn, paper["id"])


def create_pdf_pipeline_tasks(conn, paper: dict) -> None:
    """After queueing a paper, create the next appropriate pipeline task."""
    pid = paper["id"]

    # Already parsed: go straight to full translation when missing.
    if paper.get("parse_status") == "done" and paper.get("md_path"):
        if paper.get("translate_status") != "done":
            enqueue_task(
                conn,
                paper_id=pid,
                task_type="translate_full",
                payload={"md_path": paper["md_path"]},
                input_hash=content_hash("translate_full", paper["md_path"]),
            )
        return

    if paper.get("pdf_url"):
        enqueue_task(
            conn,
            paper_id=pid,
            task_type="download_pdf",
            payload={"pdf_url": paper["pdf_url"]},
            input_hash=content_hash("download_pdf", paper["pdf_url"]),
        )
    elif paper.get("local_pdf"):
        enqueue_task(
            conn,
            paper_id=pid,
            task_type="parse_pdf",
            payload={"pdf_path": paper["local_pdf"], "sha256": paper.get("pdf_sha256", "")},
            input_hash=content_hash("parse_pdf", paper.get("pdf_sha256", "")),
        )
    elif paper.get("pdf_status") in ("none", "", None):
        set_pdf_status(conn, pid, "needs_upload")


__all__ = [
    "file_sha256",
    "validate_pdf",
    "find_paper_by_title",
    "download_pdf",
    "ingest_uploaded_pdf",
    "create_pdf_pipeline_tasks",
]
