"""Wave 0 contract tests: config / paths / db / tasks / storage / llm types."""
from __future__ import annotations

import json

import pytest

from paperbase import config as cfg
from paperbase import tasks
from paperbase.db import (
    get_paper,
    init_db,
    set_note,
    set_user_tags,
    upsert_paper,
)
from paperbase.llm import LLMMessage, MockLLMClient, OpenAICompatibleClient
from paperbase.paths import PaperPaths
from paperbase.storage import (
    FilesystemObjectStore,
    ensure_capacity,
    lru_candidates,
)


@pytest.fixture()
def paths(tmp_path):
    p = PaperPaths(tmp_path / "data")
    p.ensure_dirs()
    return p


@pytest.fixture()
def conn(paths):
    return init_db(paths.db_path)


def test_config_defaults():
    c = cfg.load_config()
    assert c["fetch"]["schedule"] == "07:30"
    assert c["dci"]["max_tool_calls"] == 30
    assert c["pdf"]["hot_quota_gb"] == 6


def test_path_contract(paths):
    paper = {"id": "2026.findings-acl.38", "source": "acl", "year": 2026, "venue": "findings-acl"}
    assert paths.paper_md_rel(paper) == "acl/2026/findings-acl/2026.findings-acl.38.md"
    assert paths.paper_zh_rel(paper) == "acl/2026/findings-acl/2026.findings-acl.38.zh.md"
    # DOI style ids must not create nested directories inside the filename
    paper2 = {"id": "10.1109/tkde.2025.1", "source": "openalex", "year": 2025, "venue": "TKDE"}
    rel = paths.paper_md_rel(paper2)
    assert "/10.1109_tkde.2025.1.md" in rel
    assert paths.corpus_dir() == paths.md_dir


def test_db_upsert_preserves_user_fields(conn):
    pid = "2026.acl-long.1"
    upsert_paper(conn, {
        "id": pid, "source": "acl", "title": "A Paper",
        "authors": ["A", "B"], "abstract": "old abstract", "year": 2026,
        "venue": "acl-long", "url": "https://x/1", "pdf_url": "https://x/1.pdf",
        "doi": "", "tags": ["kg"],
    })
    set_note(conn, pid, "my note")
    set_user_tags(conn, pid, ["重点", "survey"])

    upsert_paper(conn, {
        "id": pid, "source": "acl", "title": "A Paper (updated)",
        "authors": ["A"], "abstract": "new abstract", "year": 2026,
        "venue": "acl-long", "url": "https://x/1", "pdf_url": "",
        "doi": "", "tags": ["rag"],
    })
    p = get_paper(conn, pid)
    assert p["title"] == "A Paper (updated)"
    assert p["abstract"] == "new abstract"
    assert p["note"] == "my note"
    assert p["user_tags"] == ["重点", "survey"]


def test_bulk_upsert_and_local_file_preservation(conn, paths):
    from paperbase.db import bulk_upsert_papers, set_local_file

    rows = [
        {"id": f"p{i}", "source": "acl", "title": f"Paper {i}", "year": 2026, "venue": "x"}
        for i in range(10)
    ]
    assert bulk_upsert_papers(conn, rows) == 10
    assert bulk_upsert_papers(conn, rows) == 10  # idempotent

    set_local_file(conn, "p1", md_path="acl/2026/x/p1.md")
    p = get_paper(conn, "p1")
    assert p["md_path"] == "acl/2026/x/p1.md"
    set_local_file(conn, "p1", md_zh_path="acl/2026/x/p1.zh.md")
    p = get_paper(conn, "p1")
    assert p["md_path"] == "acl/2026/x/p1.md"  # preserved
    assert p["md_zh_path"] == "acl/2026/x/p1.zh.md"


def test_tasks_idempotent_and_claim_atomic(conn):
    t1 = tasks.enqueue_task(conn, paper_id="p1", task_type="parse_pdf", payload={"sha": "abc"})
    t2 = tasks.enqueue_task(conn, paper_id="p1", task_type="parse_pdf", payload={"sha": "abc"})
    assert t1 == t2

    t3 = tasks.enqueue_task(conn, paper_id="p1", task_type="parse_pdf", payload={"sha": "def"})
    assert t3 != t1

    claimed = tasks.claim_next_task(conn, task_type="parse_pdf", limit=1)
    assert len(claimed) == 1
    assert claimed[0]["status"] == "running"
    assert claimed[0]["attempts"] == 1

    tasks.fail_task(conn, claimed[0]["id"], "boom")
    row = conn.execute("SELECT status, last_error, attempts FROM tasks WHERE id=?", (claimed[0]["id"],)).fetchone()
    # First failure requeues for retry; attempts remain recorded.
    assert row["status"] == "queued"
    assert row["last_error"] == "boom"
    assert row["attempts"] == 1
    # A later claim resets the transient error and increments attempts.
    again = tasks.claim_next_task(conn, task_type="parse_pdf", limit=10)
    hit = next(t for t in again if t["id"] == claimed[0]["id"])
    assert hit["attempts"] == 2
    assert hit["last_error"] == ""


def test_tasks_reset_running(conn):
    t = tasks.enqueue_task(conn, paper_id="p1", task_type="translate_meta")
    tasks.claim_next_task(conn, task_type="translate_meta")
    assert tasks.reset_running_tasks(conn) == 1
    assert tasks.pending_count(conn, "translate_meta") == 1


def test_filesystem_object_store_and_quota(tmp_path):
    root = tmp_path / "cold"
    store = FilesystemObjectStore(root)
    src = tmp_path / "a.pdf"
    src.write_bytes(b"hello")
    store.put("pdf/a.pdf", str(src))
    assert store.exists("pdf/a.pdf")
    dst = tmp_path / "b.pdf"
    store.get("pdf/a.pdf", str(dst))
    assert dst.read_bytes() == b"hello"

    hot = tmp_path / "hot"
    hot.mkdir()
    (hot / "x.pdf").write_bytes(b"123")
    ensure_capacity(hot, 1, 5)  # 3 existing + 1 incoming <= 5: OK
    with pytest.raises(Exception):
        ensure_capacity(hot, 1, 3)  # 3 existing + 1 incoming > 3: block
    # LRU candidates cover the needed bytes
    assert [p.name for p in lru_candidates(hot, 2)] == ["x.pdf"]


def test_llm_client_serializes_tool_calls_without_network(monkeypatch):
    c = OpenAICompatibleClient(base_url="http://127.0.0.1:1/v1", api_key="x", model="m")
    payload = None

    class FakeResp:
        status_code = 200

        def json(self):
            return {
                "choices": [{
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [{
                            "id": "call_1",
                            "function": {"name": "rg", "arguments": '{"pattern":"KBQA"}'},
                        }],
                    },
                    "finish_reason": "tool_calls",
                }],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
            }

    monkeypatch.setattr(c.session, "post", lambda *a, **kw: FakeResp())
    resp = c.chat(
        [LLMMessage(role="user", content="q")],
        tools=[{"type": "function", "function": {"name": "rg"}}],
        budget_tag="qa",
    )
    assert resp.tool_calls[0].name == "rg"
    assert resp.usage.total_tokens == 12


def test_mock_llm_contract():
    mock = MockLLMClient()
    resp = mock.chat([LLMMessage(role="user", content="hi")])
    assert resp.content == ""
    assert mock.calls
