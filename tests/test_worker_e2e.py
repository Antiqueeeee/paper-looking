"""Phase 7 tests: daily pipeline, task loop and end-to-end flow."""
from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from paperbase.db import init_db, set_local_file, set_meta, upsert_paper, get_paper
from paperbase.llm import LLMResponse, LLMUsage, MockLLMClient
from paperbase.paths import PaperPaths
from paperbase.pipeline.digest import queue_papers
from paperbase.pipeline.worker import check_disk_policy, run_daily_pipeline, run_task_loop
from paperbase.sources import FetchReport


@pytest.fixture()
def conn(tmp_path):
    return init_db(tmp_path / "papers.db")


@pytest.fixture()
def paths(tmp_path):
    p = PaperPaths(tmp_path / "data")
    p.ensure_dirs()
    return p


def test_daily_pipeline_fetch_then_digest(conn, paths, tmp_path, monkeypatch):
    import paperbase.sources as sources

    def fake_fetch(conn_, config, name, **kw):
        upsert_paper(conn_, {
            "id": f"{name}-1", "source": name, "title": "GraphRAG for Knowledge Graph QA",
            "abstract": "retrieval augmented generation", "year": 2026, "venue": "test",
        })
        return FetchReport(source=name, status="success", drafts=1)

    monkeypatch.setattr(sources, "fetch_source", fake_fetch)
    set_meta(conn, "digest:last_run", "2000-01-01T00:00:00+00:00")
    config = {
        "paths": {"data_dir": str(paths.root)},
        "fetch": {"sources": ["acl"], "years": [2026]},
        "budgets": {"translate_daily_tokens": 0},
    }
    result = run_daily_pipeline(conn, config, paths, translate=False)
    assert result["digest"]["matched"] == 1
    digest_file = Path(result["digest"]["path"])
    assert digest_file.exists()
    assert "GraphRAG" in digest_file.read_text(encoding="utf-8")


def test_disk_policy_thresholds(conn, paths, monkeypatch):
    from paperbase.pipeline import worker as worker_mod

    monkeypatch.setattr(worker_mod, "disk_usage_ratio", lambda p: 0.95)
    assert check_disk_policy(conn, {"storage": {}}, paths) == "block"
    monkeypatch.setattr(worker_mod, "disk_usage_ratio", lambda p: 0.85)
    assert check_disk_policy(conn, {"storage": {}}, paths) == "warn"
    monkeypatch.setattr(worker_mod, "disk_usage_ratio", lambda p: 0.5)
    assert check_disk_policy(conn, {"storage": {}}, paths) == "ok"


class FakeMinerU:
    def __init__(self, text):
        self.text = text

    def submit_file(self, pdf_path, data_id):
        return "batch-1"

    def wait_batch(self, batch_id):
        return {"state": "done", "full_zip_url": "https://x/result.zip"}

    def download_zip(self, url, dest):
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(dest, "w") as zf:
            zf.writestr("full.md", self.text)
        return dest


def test_end_to_end_queue_parse_translate_ask(conn, paths, tmp_path, monkeypatch):
    import paperbase.pipeline.handlers as handlers
    from paperbase.pipeline.mineru import run_parse_task as real_parse
    from paperbase.pipeline.fulltext_translate import run_translate_full_task as real_translate
    from paperbase.dci.agent import DCIQAAgent

    pdf = tmp_path / "p1.pdf"
    pdf.write_bytes(b"%PDF-1.4\n" + b"x" * 300)
    upsert_paper(conn, {
        "id": "p1", "source": "manual", "title": "GraphRAG Survey", "year": 2026,
        "venue": "test", "abstract": "retrieval augmented generation",
    })
    set_local_file(conn, "p1", local_pdf=str(pdf), pdf_sha256="abc")

    fake_mineru = FakeMinerU("## Method\n\nGraphRAG combines retrieval with generation. " * 10)
    monkeypatch.setattr(
        handlers, "run_parse_task",
        lambda c, cfg, p, task: real_parse(c, {"mineru": {"min_md_chars": 200}, "budgets": {}}, p, task, client=fake_mineru),
    )
    translate_client = MockLLMClient([
        LLMResponse(content="## 方法\n\nGraphRAG 结合检索与生成。", usage=LLMUsage(total_tokens=100))
    ])
    monkeypatch.setattr(
        handlers, "run_translate_full_task",
        lambda c, cfg, p, task: real_translate(
            c, {"budgets": {"translate_daily_tokens": 1000000}, "translation": {}}, p, task, client=translate_client
        ),
    )

    assert queue_papers(conn, ["p1"]) == 1
    processed = run_task_loop(conn, {"storage": {}, "budgets": {}}, paths)
    assert processed == 2  # parse_pdf + translate_full

    paper = get_paper(conn, "p1")
    assert paper["parse_status"] == "done"
    assert paper["translate_status"] == "done"
    assert Path(paper["md_path"]).exists()
    assert Path(paper["md_zh_path"]).exists()

    qa_client = MockLLMClient([
        LLMResponse(content="Explanation: 方法见 [p1.md:4]\nExact Answer: GraphRAG\nConfidence: 90%",
                    usage=LLMUsage(total_tokens=50))
    ])
    ans = DCIQAAgent(conn, {"dci": {"max_tool_calls": 5}}, paths, qa_client).ask(
        "方法是什么？", mode="paper", paper_ids=["p1"]
    )
    assert ans.confidence == 0.9
    assert "[p1.md:4]" in ans.citations
    assert conn.execute("SELECT COUNT(*) FROM qa_logs").fetchone()[0] == 1
