"""Phase 5 tests: DCI tool safety and Q&A loop."""
from __future__ import annotations

import json

import pytest

from paperbase.dci.agent import DCIQAAgent
from paperbase.dci.tools import ToolContext, execute_tool, sqlite_query
from paperbase.db import init_db, set_local_file, set_parse_status, upsert_paper, get_paper
from paperbase.llm import LLMResponse, LLMUsage, MockLLMClient
from paperbase.paths import PaperPaths


@pytest.fixture()
def conn(tmp_path):
    return init_db(tmp_path / "papers.db")


@pytest.fixture()
def paths(tmp_path):
    p = PaperPaths(tmp_path / "data")
    p.ensure_dirs()
    return p


def _write_paper_md(conn, paths, pid, body, tags=None, year=2026, venue="findings-acl"):
    upsert_paper(conn, {
        "id": pid, "source": "acl", "title": f"Title {pid}", "year": year,
        "venue": venue, "tags": tags or [],
    })
    set_parse_status(conn, pid, "done")
    md = paths.paper_md(get_paper(conn, pid))
    md.parent.mkdir(parents=True, exist_ok=True)
    md.write_text(f"---\ntitle: Title {pid}\n---\n{body}\n", encoding="utf-8")
    set_local_file(conn, pid, md_path=str(md))
    return md


class ScriptedClient(MockLLMClient):
    model = "mock"


def _tool_resp(name, args):
    return LLMResponse(
        content="",
        tool_calls=[__import__("paperbase.llm", fromlist=["ToolCall"]).ToolCall("c1", name, json.dumps(args))],
        finish_reason="tool_calls",
        usage=LLMUsage(prompt_tokens=10, completion_tokens=2, total_tokens=12),
    )


def _final_resp(text):
    return LLMResponse(content=text, usage=LLMUsage(prompt_tokens=20, completion_tokens=10, total_tokens=30))


def test_single_paper_qa_loop(conn, paths):
    _write_paper_md(conn, paths, "p1", "Our GraphRAG method indexes entities.\nSecond line.\nThird line.\n")
    client = ScriptedClient([
        _tool_resp("rg", {"pattern": "GraphRAG", "context_lines": 1}),
        _final_resp("Explanation: found method [p1.md:1]\nExact Answer: GraphRAG\nConfidence: 90%"),
    ])
    ans = DCIQAAgent(conn, {"dci": {"max_tool_calls": 30, "tool_output_chars": 12000}}, paths, client).ask(
        "What is the method?", mode="paper", paper_ids=["p1"]
    )
    assert ans.answer.startswith("Explanation")
    assert "[p1.md:1]" in ans.citations
    assert ans.confidence == 0.9
    assert ans.tool_calls == 1
    row = conn.execute("SELECT * FROM qa_logs ORDER BY id DESC LIMIT 1").fetchone()
    assert row["mode"] == "paper"
    assert json.loads(row["paper_ids"]) == ["p1"]


def test_library_scope_restricts_rg(conn, paths):
    _write_paper_md(conn, paths, "p1", "Knowledge Graph embedding works.\n", tags=["kg"])
    _write_paper_md(conn, paths, "p2", "Knowledge Graph embedding but out of scope.\n", tags=[])
    client = ScriptedClient([
        _tool_resp("rg", {"pattern": "Knowledge Graph", "path": ""}),
        _final_resp("Explanation: [p1.md:1]\nExact Answer: p1\nConfidence: 80%"),
    ])
    ans = DCIQAAgent(conn, {"dci": {"max_tool_calls": 30}}, paths, client).ask(
        "2026 knowledge graph methods", mode="library"
    )
    # The tool result embedded in the second LLM call contains only scoped output.
    tool_message = client.calls[1]["messages"][-1]["content"]
    assert "p1.md" in tool_message
    assert "p2.md" not in tool_message
    assert ans.citations == ["[p1.md:1]"]


def test_no_evidence_low_confidence(conn, paths):
    _write_paper_md(conn, paths, "p1", "Nothing relevant.\n", tags=["kg"])
    client = ScriptedClient([_final_resp("Explanation: no evidence\nExact Answer: 未找到足够证据\nConfidence: 10%")])
    ans = DCIQAAgent(conn, {"dci": {"max_tool_calls": 5}}, paths, client).ask(
        "question about missing topic", mode="paper", paper_ids=["p1"]
    )
    assert ans.confidence == 0.1
    assert "未找到足够证据" in ans.answer


def test_tool_safety(conn, paths):
    ctx = ToolContext(corpus_dir=paths.md_dir, db_path=paths.db_path)
    # sqlite only SELECT
    assert "only SELECT" in sqlite_query(ctx, "DELETE FROM papers")
    # path traversal
    outside = paths.root.parent / "secret.md"
    outside.write_text("secret", encoding="utf-8")
    assert "outside corpus" in execute_tool("read_file", {"path": str(outside), "start_line": 1, "end_line": 1}, ctx)
    # scope restriction
    paths.md_dir.mkdir(parents=True, exist_ok=True)
    a = paths.md_dir / "a.md"; a.write_text("hello\n", encoding="utf-8")
    b = paths.md_dir / "b.md"; b.write_text("other\n", encoding="utf-8")
    scoped = ToolContext(corpus_dir=paths.md_dir, db_path=paths.db_path, scope_files=[a])
    assert "outside current question scope" in execute_tool("read_file", {"path": "b.md", "start_line": 1, "end_line": 1}, scoped)


def test_ask_carries_conversation_history(conn, paths):
    _write_paper_md(conn, paths, "p1", "Some text about methods.\n", tags=[])
    client = ScriptedClient([_final_resp("Explanation: [p1.md:1]\nExact Answer: ok\nConfidence: 80%")])
    ans = DCIQAAgent(conn, {"dci": {"max_tool_calls": 5}}, paths, client).ask(
        "它和之前那个有什么区别？",
        mode="paper",
        paper_ids=["p1"],
        history=[{"question": "方法是什么？", "answer": "使用GraphRAG。"}],
    )
    assert ans.confidence == 0.8
    sent = client.calls[0]["messages"][1]["content"]
    assert "方法是什么？" in sent
    assert "使用GraphRAG" in sent
    assert "当前问题：它和之前那个有什么区别？" in sent
