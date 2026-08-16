"""Dispatch claimed tasks to their handlers."""
from __future__ import annotations

from paperbase.db import get_paper, set_pdf_status
from paperbase.paths import PaperPaths
from paperbase.tasks import enqueue_task, fail_task, finish_task, content_hash
from paperbase.pipeline.fulltext_translate import run_translate_full_task
from paperbase.pipeline.mineru import run_parse_task
from paperbase.pipeline.pdf import download_pdf
from paperbase.pipeline.storage_manager import get_object_store


def run_download_task(conn, config: dict, paths: PaperPaths, task: dict) -> None:
    task_id = int(task["id"])
    paper_id = task["paper_id"]
    paper = get_paper(conn, paper_id)
    if paper is None:
        fail_task(conn, task_id, f"paper {paper_id} not found")
        return
    store = get_object_store(config, paths)
    try:
        dest = download_pdf(conn, paper, paths, config, store=store)
        enqueue_task(
            conn,
            paper_id=paper_id,
            task_type="parse_pdf",
            payload={"pdf_path": str(dest)},
            input_hash=content_hash("parse_pdf", paper.get("pdf_sha256") or dest.name),
        )
        finish_task(conn, task_id, {"pdf_path": str(dest)})
    except Exception as exc:
        fail_task(conn, task_id, str(exc))
        row = conn.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row and row["status"] == "failed":
            set_pdf_status(conn, paper_id, "download_failed")


def process_task(conn, config: dict, paths: PaperPaths, task: dict) -> str:
    """Run one claimed task. Returns task_type."""
    task_type = task["task_type"]
    if task_type == "download_pdf":
        run_download_task(conn, config, paths, task)
    elif task_type == "parse_pdf":
        run_parse_task(conn, config, paths, task)
    elif task_type == "translate_full":
        run_translate_full_task(conn, config, paths, task)
    elif task_type == "translate_meta":
        # Metadata translation is driven by digest directly.
        finish_task(conn, int(task["id"]), {"skipped": True})
    else:
        fail_task(conn, int(task["id"]), f"unknown task type: {task_type}")
    return task_type


__all__ = ["run_download_task", "process_task"]
