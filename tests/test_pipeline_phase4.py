"""Phase 4 tests: full-text translation chunking, cache and task handler."""
from __future__ import annotations

import pytest

from paperbase.db import init_db, set_local_file, upsert_paper, get_paper
from paperbase.llm import LLMResponse, LLMUsage, MockLLMClient
from paperbase.paths import PaperPaths
from paperbase.pipeline.fulltext_translate import (
    run_translate_full_task,
    split_markdown_chunks,
)
from paperbase.tasks import enqueue_task, task_to_dict


@pytest.fixture()
def conn(tmp_path):
    return init_db(tmp_path / "papers.db")


@pytest.fixture()
def paths(tmp_path):
    p = PaperPaths(tmp_path / "data")
    p.ensure_dirs()
    return p


def test_split_markdown_chunks():
    text = "\n\n".join(f"## Section {i}\n" + "x" * 500 for i in range(20))
    chunks = split_markdown_chunks(text, max_chars=1200, min_chars=400)
    assert len(chunks) > 1
    assert all(len(c) <= 1400 for c in chunks)


def test_full_translation_cache_and_handler(conn, paths):
    upsert_paper(conn, {
        "id": "p1", "source": "acl", "title": "Paper One", "year": 2026, "venue": "x",
    })
    src = paths.paper_md(get_paper(conn, "p1"))
    src.parent.mkdir(parents=True, exist_ok=True)
    body = "## Abstract\n\nGraphRAG combines retrieval and generation.\n\n## Method\n\nWe propose a KBQA method."
    src.write_text(f"---\ntitle: Paper One\n---\n{body}\n", encoding="utf-8")
    set_local_file(conn, "p1", md_path=str(src))

    client = MockLLMClient([
        LLMResponse(
            content="## 摘要\n\nGraphRAG 结合了检索与生成。",
            usage=LLMUsage(prompt_tokens=100, completion_tokens=50, total_tokens=150),
        )
    ])
    config = {"budgets": {"translate_daily_tokens": 1000000}}

    tid = enqueue_task(conn, paper_id="p1", task_type="translate_full", payload={"md_path": str(src)})
    task = task_to_dict(conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone())
    run_translate_full_task(conn, config, paths, task, client=client)

    paper = get_paper(conn, "p1")
    assert paper["translate_status"] == "done"
    assert paper["md_zh_path"].endswith(".zh.md")
    zh = open(paper["md_zh_path"], encoding="utf-8").read()
    assert "GraphRAG 结合了检索与生成" in zh
    assert zh.startswith("---\n")  # front matter preserved
    assert conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()[0] == "done"

    # A second task for the same content hits the cache without another LLM call.
    tid2 = enqueue_task(
        conn, paper_id="p1", task_type="translate_full",
        payload={"md_path": str(src), "retry": 1},
    )
    task2 = task_to_dict(conn.execute("SELECT * FROM tasks WHERE id=?", (tid2,)).fetchone())
    run_translate_full_task(conn, config, paths, task2, client=client)
    assert conn.execute("SELECT status FROM tasks WHERE id=?", (tid2,)).fetchone()[0] == "done"
    assert len(client.calls) == 1


def test_full_translation_budget_blocks(conn, paths):
    upsert_paper(conn, {
        "id": "p1", "source": "acl", "title": "Paper One", "year": 2026, "venue": "x",
    })
    src = paths.paper_md(get_paper(conn, "p1"))
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text("## Method\n" + "x" * 2000, encoding="utf-8")
    set_local_file(conn, "p1", md_path=str(src))

    client = MockLLMClient()
    tid = enqueue_task(conn, paper_id="p1", task_type="translate_full", payload={"md_path": str(src)})
    task = task_to_dict(conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone())
    run_translate_full_task(conn, {"budgets": {"translate_daily_tokens": 1}}, paths, task, client=client)
    row = conn.execute("SELECT status, last_error FROM tasks WHERE id=?", (tid,)).fetchone()
    assert row["status"] == "queued"
    assert "budget" in row["last_error"]
    assert len(client.calls) == 0
