"""MinerU precise-parse API client and parse task handler.

Protocol (documented at https://mineru.net/apiManage/docs):
  1. POST /api/v4/file-urls/batch   -> batch_id + signed upload URLs
  2. PUT  local PDF to signed URL
  3. GET  /api/v4/extract-results/batch/{batch_id}
  4. when state=done download full_zip_url and extract full.md
"""
from __future__ import annotations

import logging
import os
import shutil
import time
import zipfile
from pathlib import Path

import requests
import yaml

from paperbase.db import get_paper, set_local_file, set_parse_status, utcnow
from paperbase.paths import PaperPaths, safe_component
from paperbase.tasks import enqueue_task, fail_task, finish_task, release_task

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://mineru.net/api/v4"


class MinerUError(RuntimeError):
    pass


class MinerUClient:
    def __init__(self, config: dict):
        mineru_cfg = config.get("mineru", {})
        self.base_url = (mineru_cfg.get("base_url") or DEFAULT_BASE_URL).rstrip("/")
        token = os.environ.get(mineru_cfg.get("api_key_env", "MINERU_API_KEY"), "")
        if not token:
            raise MinerUError(
                f"MinerU token not set: export {mineru_cfg.get('api_key_env', 'MINERU_API_KEY')}"
            )
        self.token = token
        self.model_version = mineru_cfg.get("model_version", "vlm")
        self.language = mineru_cfg.get("language", "en")
        self.enable_formula = bool(mineru_cfg.get("enable_formula", True))
        self.enable_table = bool(mineru_cfg.get("enable_table", True))
        self.poll_interval = int(mineru_cfg.get("poll_interval_seconds", 5))
        self.max_poll_seconds = int(mineru_cfg.get("max_poll_seconds", 3600))
        self.session = requests.Session()

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}

    def _check(self, payload: dict) -> dict:
        if not isinstance(payload, dict):
            raise MinerUError(f"unexpected MinerU response: {payload!r}")
        if payload.get("code") != 0:
            raise MinerUError(f"MinerU code={payload.get('code')} msg={payload.get('msg')}")
        return payload.get("data") or {}

    def submit_file(self, pdf_path: str | Path, data_id: str) -> str:
        """Upload one local PDF and return batch_id."""
        path = Path(pdf_path)
        payload = {
            "files": [{
                "name": path.name,
                "data_id": safe_component(data_id)[:128],
                "is_ocr": False,
            }],
            "model_version": self.model_version,
            "enable_formula": self.enable_formula,
            "enable_table": self.enable_table,
            "language": self.language,
        }
        resp = self.session.post(
            f"{self.base_url}/file-urls/batch", headers=self._headers(), json=payload, timeout=60
        )
        resp.raise_for_status()
        data = self._check(resp.json())
        batch_id = data.get("batch_id")
        urls = data.get("file_urls") or []
        if not batch_id or not urls:
            raise MinerUError(f"MinerU batch upload missing batch_id/file_urls: {data}")

        with open(path, "rb") as f:
            up = requests.put(urls[0], data=f, timeout=600)
        if up.status_code not in (200, 201, 204):
            raise MinerUError(f"MinerU PUT upload failed: HTTP {up.status_code}: {up.text[:200]}")
        return batch_id

    def poll_batch(self, batch_id: str) -> dict:
        resp = self.session.get(
            f"{self.base_url}/extract-results/batch/{batch_id}", headers=self._headers(), timeout=60
        )
        resp.raise_for_status()
        data = self._check(resp.json())
        results = data.get("extract_result") or []
        if not results:
            raise MinerUError(f"MinerU batch {batch_id} returned no extract_result")
        return results[0]

    def wait_batch(self, batch_id: str) -> dict:
        deadline = time.time() + self.max_poll_seconds
        while time.time() < deadline:
            result = self.poll_batch(batch_id)
            state = result.get("state", "")
            if state == "done":
                return result
            if state == "failed":
                raise MinerUError(f"MinerU parse failed: {result.get('err_msg', 'unknown')}")
            time.sleep(self.poll_interval)
        raise MinerUError(f"MinerU parse timed out after {self.max_poll_seconds}s")

    def download_zip(self, url: str, dest: str | Path) -> Path:
        resp = requests.get(url, timeout=300, stream=True)
        resp.raise_for_status()
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(".zip.part")
        with open(tmp, "wb") as f:
            for chunk in resp.iter_content(1024 * 256):
                if chunk:
                    f.write(chunk)
        shutil.move(str(tmp), dest)
        return dest


def extract_full_md(zip_path: str | Path, min_chars: int = 200) -> str:
    """Find and return full.md content from a MinerU result zip."""
    with zipfile.ZipFile(zip_path) as zf:
        names = [n for n in zf.namelist() if n.endswith("full.md")]
        if not names:
            raise MinerUError("MinerU zip contains no full.md")
        text = zf.read(names[0]).decode("utf-8", errors="replace")
    if len(text.strip()) < min_chars:
        raise MinerUError(f"MinerU markdown too short: {len(text.strip())} chars < {min_chars}")
    return text


def build_front_matter(paper: dict) -> str:
    data = {
        "id": paper.get("id", ""),
        "title": paper.get("title", ""),
        "authors": paper.get("authors", []),
        "year": paper.get("year"),
        "venue": paper.get("venue", ""),
        "source": paper.get("source", ""),
        "tags": paper.get("tags", []),
        "url": paper.get("url", ""),
        "pdf_url": paper.get("pdf_url", ""),
    }
    fm = yaml.safe_dump(data, allow_unicode=True, sort_keys=False).strip()
    return f"---\n{fm}\n---\n"


def write_markdown_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".md.part")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def daily_parse_count(conn, now: str | None = None) -> int:
    now = now or utcnow()
    day_start = now[:10] + "T00:00:00"
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM tasks WHERE task_type='parse_pdf' AND status='done' AND created_at>=?",
        (day_start,),
    ).fetchone()
    return int(row["n"])


def run_parse_task(
    conn,
    config: dict,
    paths: PaperPaths,
    task: dict,
    *,
    client: MinerUClient | None = None,
    store=None,
) -> None:
    """Execute one claimed parse_pdf task end-to-end."""
    task_id = int(task["id"])
    paper_id = task["paper_id"]
    paper = get_paper(conn, paper_id)
    if paper is None:
        fail_task(conn, task_id, f"paper {paper_id} not found")
        return

    payload = task.get("payload") or {}
    pdf_path = Path(payload.get("pdf_path") or paper.get("local_pdf") or "")

    if not pdf_path.exists():
        if paper.get("object_key") and store is not None:
            from paperbase.pipeline.storage_manager import restore_cold_pdf

            restored = restore_cold_pdf(conn, paths, store, paper_id)
            pdf_path = restored or pdf_path
        if not pdf_path.exists():
            fail_task(conn, task_id, f"PDF not found: {pdf_path}")
            set_parse_status(conn, paper_id, "failed")
            return

    limit = int(config.get("budgets", {}).get("parse_daily_count", 50))
    if limit > 0 and daily_parse_count(conn) >= limit:
        release_task(conn, task_id, "daily parse budget exhausted")
        return

    if client is None:
        client = MinerUClient(config)
    try:
        data_id = safe_component(paper_id)[:128]
        set_parse_status(conn, paper_id, "uploading")
        batch_id = client.submit_file(pdf_path, data_id)
        set_parse_status(conn, paper_id, "parsing")
        result = client.wait_batch(batch_id)
        zip_url = result.get("full_zip_url") or ""
        if not zip_url:
            raise MinerUError(f"MinerU result missing full_zip_url: {result}")

        set_parse_status(conn, paper_id, "downloading")
        tmp_dir = paths.task_tmp("parse_pdf", task_id)
        tmp_dir.mkdir(parents=True, exist_ok=True)
        zip_path = client.download_zip(zip_url, tmp_dir / "result.zip")
        md_text = extract_full_md(zip_path, int(config.get("mineru", {}).get("min_md_chars", 200)))

        paper = get_paper(conn, paper_id)  # refresh tags/authors
        md_path = paths.paper_md(paper)
        write_markdown_atomic(md_path, build_front_matter(paper) + md_text)
        set_local_file(conn, paper_id, md_path=str(md_path))
        set_parse_status(conn, paper_id, "done")
        shutil.rmtree(tmp_dir, ignore_errors=True)

        finish_task(conn, task_id, {"md_path": str(md_path), "batch_id": batch_id})
        _enqueue_full_translation_if_needed(conn, paper_id)
    except Exception as exc:
        fail_task(conn, task_id, str(exc))
        # Only mark the paper failed after the task exhausts its retry budget.
        row = conn.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row and row["status"] == "failed":
            set_parse_status(conn, paper_id, "failed")


def _enqueue_full_translation_if_needed(conn, paper_id: str) -> None:
    paper = get_paper(conn, paper_id)
    if paper and paper["status"] == "in_queue":
        enqueue_task(
            conn,
            paper_id=paper_id,
            task_type="translate_full",
            payload={"md_path": paper.get("md_path") or ""},
            input_hash=paper.get("md_path") or "",
        )


__all__ = [
    "MinerUClient",
    "MinerUError",
    "extract_full_md",
    "build_front_matter",
    "write_markdown_atomic",
    "daily_parse_count",
    "run_parse_task",
]
