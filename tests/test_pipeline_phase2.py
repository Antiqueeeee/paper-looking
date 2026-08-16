"""Phase 2 tests: interest rules, metadata translation, digest, queue."""
from __future__ import annotations

import pytest

from paperbase.db import init_db, set_meta, upsert_paper, get_paper
from paperbase.llm import LLMResponse, LLMUsage, MockLLMClient
from paperbase.pipeline.digest import build_daily_digest, queue_papers, render_digest
from paperbase.pipeline.filter import apply_rules
from paperbase.pipeline.translate import translate_meta_for_papers


@pytest.fixture()
def conn(tmp_path):
    return init_db(tmp_path / "papers.db")


def _paper(pid, title, abstract="", tags=None):
    return {
        "id": pid, "source": "acl", "title": title, "abstract": abstract,
        "year": 2026, "venue": "findings-acl", "url": f"https://x/{pid}",
        "pdf_url": f"https://x/{pid}.pdf", "tags": tags or [],
    }


def test_rules_match_and_apply(conn):
    upsert_paper(conn, _paper("p1", "GraphRAG for Knowledge Graph Question Answering", "uses retrieval augmented generation"))
    upsert_paper(conn, _paper("p2", "A Faster Tokenizer for Low Resource Languages", ""))
    changed = apply_rules(conn)
    assert changed == 1  # only p1 transitions [] -> tags
    assert get_paper(conn, "p1")["tags"] == ["kg", "kbqa", "rag"]
    assert get_paper(conn, "p2")["tags"] == []


def test_translation_cache_and_budget(conn):
    upsert_paper(conn, _paper("p1", "Knowledge Graph QA", "Abstract here."))
    apply_rules(conn)

    client = MockLLMClient([
        LLMResponse(
            content='{"title_zh":"知识图谱问答","abstract_zh":"这里是摘要。"}',
            usage=LLMUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        )
    ])
    config = {"budgets": {"translate_daily_tokens": 1000000}, "llm": {"model": "mock"}}
    stats = translate_meta_for_papers(conn, client, config, ["p1"])
    assert stats.translated == 1
    assert get_paper(conn, "p1")["title_zh"] == "知识图谱问答"

    # Same content again: cache hit, no new LLM call.
    stats2 = translate_meta_for_papers(conn, client, config, ["p1"])
    assert stats2.cached == 1
    assert len(client.calls) == 1

    # Tiny budget blocks new work before calling the model.
    upsert_paper(conn, _paper("p2", "Another Knowledge Graph paper", "Long abstract " + "x" * 500))
    apply_rules(conn)
    stats3 = translate_meta_for_papers(conn, client, {"budgets": {"translate_daily_tokens": 1}}, ["p2"])
    assert stats3.budget_blocked == 1
    assert len(client.calls) == 1


def test_digest_baseline_then_new_papers(conn, tmp_path, paths=None):
    from paperbase.paths import PaperPaths
    paths = PaperPaths(tmp_path / "data")
    paths.ensure_dirs()

    # First run establishes a baseline and must not dump 60k historical rows.
    result = build_daily_digest(conn, {"budgets": {"translate_daily_tokens": 0}}, paths, translate=False)
    assert result.baseline is True
    assert result.matched == 0

    set_meta(conn, "digest:last_run", "2000-01-01T00:00:00+00:00")
    upsert_paper(conn, _paper("p1", "GraphRAG for KBQA", "retrieval augmented"))
    apply_rules(conn)

    result = build_daily_digest(conn, {"budgets": {"translate_daily_tokens": 0}}, paths, translate=False)
    assert result.baseline is False
    assert result.matched == 1
    assert result.path.endswith(".md")
    text = open(result.path, encoding="utf-8").read()
    assert "知识图谱" in text
    assert "GraphRAG for KBQA" in text


def test_render_digest_and_queue(conn):
    upsert_paper(conn, _paper("p1", "GraphRAG for KBQA", tags=["kg", "rag"]))
    md = render_digest([get_paper(conn, "p1")])
    assert "知识图谱" in md
    assert "RAG" in md

    assert queue_papers(conn, ["p1"]) == 1
    assert get_paper(conn, "p1")["status"] == "in_queue"
    assert queue_papers(conn, ["p1"]) == 1  # idempotent state write
