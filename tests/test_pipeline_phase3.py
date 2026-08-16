"""Phase 3 tests: PDF upload/download, MinerU handler, storage eviction."""
from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from paperbase.db import init_db, upsert_paper, get_paper, set_local_file, set_parse_status, update_paper_status
from paperbase.paths import PaperPaths
from paperbase.pipeline.mineru import run_parse_task
from paperbase.pipeline.pdf import download_pdf, file_sha256, ingest_uploaded_pdf, validate_pdf
from paperbase.pipeline.storage_manager import evict_hot_pdfs, restore_cold_pdf
from paperbase.storage import FilesystemObjectStore
from paperbase.tasks import task_to_dict


@pytest.fixture()
def conn(tmp_path):
    return init_db(tmp_path / "papers.db")


@pytest.fixture()
def paths(tmp_path):
    p = PaperPaths(tmp_path / "data")
    p.ensure_dirs()
    return p


def _pdf(path: Path, size: int = 200) -> Path:
    path.write_bytes(b"%PDF-1.4\n" + b"x" * size)
    return path


def _paper(pid, **kw):
    base = {"id": pid, "source": "acl", "title": f"Paper {pid}", "year": 2026, "venue": "x"}
    base.update(kw)
    return base


def test_pdf_validation_and_upload_ingest(conn, paths, tmp_path):
    good = _pdf(tmp_path / "a.pdf")
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"hello")
    ok, _ = validate_pdf(good)
    assert ok
    ok, reason = validate_pdf(bad)
    assert not ok and "bad magic" in reason

    paper = ingest_uploaded_pdf(conn, paths, {"pdf": {"max_upload_mb": 100}}, good, title="My Paper")
    assert paper["id"].startswith("manual:")
    assert paper["local_pdf"].endswith(".pdf")
    assert get_paper(conn, paper["id"])["pdf_status"] == "downloaded"
    tasks = conn.execute("SELECT * FROM tasks WHERE paper_id=?", (paper["id"],)).fetchall()
    assert [t["task_type"] for t in tasks] == ["parse_pdf"]

    # Duplicate content must not create a second record.
    paper2 = ingest_uploaded_pdf(conn, paths, {"pdf": {"max_upload_mb": 100}}, good)
    assert paper2["id"] == paper["id"]
    assert conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0] == 1


def test_download_pdf_success(conn, paths, monkeypatch):
    upsert_paper(conn, _paper("p1", pdf_url="https://example.org/p1.pdf"))

    class Resp:
        headers = {"Content-Length": "1000"}
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def raise_for_status(self): pass
        def iter_content(self, size):
            yield b"%PDF-1.4\n" + b"y" * 100

    monkeypatch.setattr("paperbase.pipeline.pdf.requests.get", lambda *a, **kw: Resp())
    paper = get_paper(conn, "p1")
    dest = download_pdf(conn, paper, paths, {"pdf": {"hot_quota_gb": 6, "download_retries": 0}})
    assert dest.exists()
    assert get_paper(conn, "p1")["pdf_status"] == "downloaded"
    assert get_paper(conn, "p1")["pdf_sha256"] == file_sha256(dest)


class FakeMinerU:
    def __init__(self, md_text="This is a parsed paper body with enough characters. " * 10):
        self.md_text = md_text
        self.submitted = []

    def submit_file(self, pdf_path, data_id):
        self.submitted.append((str(pdf_path), data_id))
        return "batch-1"

    def wait_batch(self, batch_id):
        return {"state": "done", "full_zip_url": "https://example.org/result.zip"}

    def download_zip(self, url, dest):
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(dest, "w") as zf:
            zf.writestr("full.md", self.md_text)
        return dest


def test_run_parse_task_success(conn, paths, tmp_path):
    pdf = _pdf(tmp_path / "p1.pdf")
    upsert_paper(conn, _paper("p1"))
    update_paper_status(conn, "p1", "in_queue")
    set_local_file(conn, "p1", local_pdf=str(pdf))
    from paperbase.tasks import enqueue_task
    tid = enqueue_task(conn, paper_id="p1", task_type="parse_pdf", payload={"pdf_path": str(pdf)})
    task = task_to_dict(conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone())

    run_parse_task(conn, {"mineru": {"min_md_chars": 200}}, paths, task, client=FakeMinerU())

    paper = get_paper(conn, "p1")
    assert paper["parse_status"] == "done"
    assert Path(paper["md_path"]).exists()
    assert "This is a parsed paper body" in Path(paper["md_path"]).read_text(encoding="utf-8")
    assert conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()[0] == "done"
    queued = conn.execute("SELECT task_type FROM tasks WHERE paper_id='p1' AND status='queued'").fetchall()
    assert "translate_full" in [t[0] for t in queued]


def test_run_parse_task_short_result_requeues(conn, paths, tmp_path):
    pdf = _pdf(tmp_path / "p1.pdf")
    upsert_paper(conn, _paper("p1"))
    set_local_file(conn, "p1", local_pdf=str(pdf))
    from paperbase.tasks import enqueue_task
    tid = enqueue_task(conn, paper_id="p1", task_type="parse_pdf", payload={"pdf_path": str(pdf)})
    task = task_to_dict(conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone())

    run_parse_task(conn, {"mineru": {"min_md_chars": 200}}, paths, task, client=FakeMinerU(md_text="too short"))
    row = conn.execute("SELECT status, last_error FROM tasks WHERE id=?", (tid,)).fetchone()
    assert row["status"] == "queued"
    assert "too short" in row["last_error"]


def test_storage_eviction_skips_unparsed_and_restores(conn, paths):
    store = FilesystemObjectStore(paths.root / "cold")
    hot1 = _pdf(paths.hot_pdf("p1"))
    hot2 = _pdf(paths.hot_pdf("p2"))
    upsert_paper(conn, _paper("p1"))
    upsert_paper(conn, _paper("p2"))
    set_parse_status(conn, "p1", "done")
    set_parse_status(conn, "p2", "none")
    set_local_file(conn, "p1", local_pdf=str(hot1))
    set_local_file(conn, "p2", local_pdf=str(hot2))

    n = evict_hot_pdfs(conn, paths, store, quota_bytes=1)
    assert n == 1
    assert not hot1.exists()
    assert hot2.exists()
    assert get_paper(conn, "p1")["object_key"] == "pdf/p1.pdf"

    out = restore_cold_pdf(conn, paths, store, "p1")
    assert out == paths.hot_pdf("p1")
    assert out.exists()
    assert get_paper(conn, "p1")["pdf_status"] == "downloaded"
