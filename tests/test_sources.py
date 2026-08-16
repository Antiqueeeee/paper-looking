"""Phase 1 tests: legacy import, ACL parser, OpenAlex mapping, arXiv parser."""
from __future__ import annotations

import json

import pytest

from paperbase.db import count_papers, get_paper, init_db, upsert_paper
from paperbase.models import PaperDraft
from paperbase.sources.acl import parse_volume_papers
from paperbase.sources.arxiv import parse_arxiv_atom
from paperbase.sources.import_legacy import discover_legacy_files, import_legacy
from paperbase.sources.openalex import rebuild_abstract, work_to_draft


@pytest.fixture()
def conn(tmp_path):
    return init_db(tmp_path / "papers.db")


def test_legacy_import_dedup_and_report(tmp_path, conn):
    d = tmp_path / "legacy"
    d.mkdir()
    (d / "2026.jsonl").write_text("\n".join([
        json.dumps({"id": "2026.acl-long.1", "title": "Paper One", "authors": ["A"],
                    "abstract": "abs", "url": "https://x/1", "pdf_url": "https://x/1.pdf",
                    "year": "2026", "volume": "acl-long"}),
        json.dumps({"id": "2026.acl-long.1", "title": "Paper One duplicate", "year": "2026"}),
        json.dumps({"id": "", "title": "no id", "year": "2026"}),
        "not-json",
    ]), encoding="utf-8")
    (d / "journals_2025_2026.jsonl").write_text("\n".join([
        json.dumps({"id": "10.1/X", "title": "Journal Paper", "year": "2025",
                    "volume": "TKDE", "tags": ["kg"]}),
        json.dumps({"id": "10.1/X", "title": "Journal Paper", "year": "2025",
                    "volume": "TKDE", "tags": ["kg"]}),
    ]), encoding="utf-8")

    assert len(discover_legacy_files(d)) == 2
    report = import_legacy(conn, d)
    assert report.rows == 4
    assert report.skipped == 2  # missing-id row + bad json row
    assert count_papers(conn) == 2  # duplicate ACL + duplicate DOI collapse
    p = get_paper(conn, "10.1/x")
    assert p["source"] == "openalex"
    assert p["tags"] == ["kg"]
    p2 = get_paper(conn, "2026.acl-long.1")
    assert p2["title"] == "Paper One"  # first record wins the imported batch


ACL_HTML = """
<html><body><div class="abstract-collapse" id="abstract-2026.acl-long--1"><div class="card-body">An abstract.</div></div>
<div class="d-sm-flex align-items-stretch mb-3">
  <a class="badge" href="https://aclanthology.org/2026.acl-long.1.pdf">pdf</a>
  <span class="d-block"><strong><a href="/2026.acl-long.1/">A Test Paper</a></strong>
  <a href="/people/a/alice">Alice</a><a href="/people/b/bob">Bob</a></span>
</div>
<div class="d-sm-flex align-items-stretch mb-3">
  <a class="badge" href="https://aclanthology.org/2026.acl-long.0.pdf">pdf</a>
  <span class="d-block"><strong><a href="/2026.acl-long.0/">Proceedings</a></strong></span>
</div>
</body></html>
"""


def test_parse_acl_volume_papers():
    from bs4 import BeautifulSoup
    papers = parse_volume_papers(BeautifulSoup(ACL_HTML, "html.parser"), "2026.acl-long")
    assert len(papers) == 1
    p = papers[0]
    assert p["id"] == "2026.acl-long.1"
    assert p["abstract"] == "An abstract."
    assert p["authors"] == ["Alice", "Bob"]
    assert p["year"] == 2026


def test_openalex_work_to_draft():
    work = {
        "id": "https://openalex.org/W123",
        "doi": "https://doi.org/10.1/ABC",
        "title": "GraphRAG for KBQA",
        "publication_year": 2026,
        "authorships": [{"author": {"display_name": "Alice A"}}, {"author": {}}],
        "abstract_inverted_index": {"Graph": [0], "RAG": [1], "works": [2]},
        "best_oa_location": {"pdf_url": "https://example.org/paper.pdf"},
    }
    d = work_to_draft(work, "TKDE")
    assert d.id == "10.1/abc"
    assert d.abstract == "Graph RAG works"
    assert d.authors == ["Alice A"]
    assert d.pdf_url == "https://example.org/paper.pdf"


def test_rebuild_abstract_sorts_positions():
    assert rebuild_abstract({"b": [2], "a": [0], "c": [1]}) == "a c b"


ATOM = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
<entry>
  <id>http://arxiv.org/abs/2601.00001v1</id>
  <title>  A Paper   Title </title>
  <summary>Abstract text.</summary>
  <author><name>Alice A</name></author>
  <published>2026-01-01T00:00:00Z</published>
  <primary_category term="cs.CL"/>
</entry>
<entry><id>bad</id><title></title></entry>
</feed>
"""


def test_parse_arxiv_atom():
    drafts = parse_arxiv_atom(ATOM)
    assert len(drafts) == 1
    d = drafts[0]
    assert d.id == "arxiv:2601.00001"
    assert d.title == "A Paper Title"
    assert d.pdf_url == "http://arxiv.org/pdf/2601.00001.pdf"


class _FakeSource:
    name = "fake"
    def __init__(self):
        self.last_errors = []
    def fetch_incremental(self, since, state):
        return [PaperDraft(id="2026.x-1.1", source="acl", title="T", year=2026)]


def test_fetch_source_orchestration(tmp_path, conn, monkeypatch):
    import paperbase.sources as sources

    fake = _FakeSource()
    monkeypatch.setattr(sources, "get_source", lambda name, config: fake)
    report = sources.fetch_source(conn, {"paths": {"data_dir": str(tmp_path)}, "fetch": {"years": [2026]}}, "fake")
    assert report.status == "success"
    assert report.drafts == 1
    assert report.after == 1
    assert get_paper(conn, "2026.x-1.1")["source"] == "acl"
    row = conn.execute("SELECT * FROM fetch_runs WHERE source='fake'").fetchone()
    assert row["status"] == "success"
    assert row["new_count"] == 1
    raw = conn.execute("SELECT value FROM meta WHERE key='source_state:fake'").fetchone()
    assert "last_success_at" in raw["value"]


def test_legacy_title_translation_import(tmp_path, conn):
    import json as _json
    from paperbase.sources.import_title_translations import (
        import_title_translations,
        load_legacy_translations,
    )

    legacy = tmp_path / "legacy"
    (legacy / "titles_zh").mkdir(parents=True)
    (legacy / "titles_zh" / "zh_0.jsonl").write_text(
        "\n".join([
            _json.dumps({"id": "2026.acl-long.1", "zh": "知识图谱论文"}),
            _json.dumps({"id": "2026.acl-long.2", "zh": "信息抽取论文"}),
        ]), encoding="utf-8"
    )
    (legacy / "interest_titles_2025_2026.txt").write_text(
        "[2026] [KBS] GraphRAG for KBQA / 面向KBQA的GraphRAG (10.1016/J.KNOSYS.2026.1)  [RAG]\n",
        encoding="utf-8"
    )

    pairs = load_legacy_translations(legacy)
    assert pairs["2026.acl-long.1"] == "知识图谱论文"
    assert pairs["10.1016/j.knosys.2026.1"] == "面向KBQA的GraphRAG"

    # DB contains p1 (empty zh) and p3 (already translated); p2 missing.
    upsert_paper(conn, {"id": "2026.acl-long.1", "source": "acl", "title": "T1"})
    upsert_paper(conn, {"id": "2026.acl-long.3", "source": "acl", "title": "T3"})
    conn.execute("UPDATE papers SET title_zh='已有翻译' WHERE id='2026.acl-long.3'")
    conn.commit()
    report = import_title_translations(conn, legacy)
    assert report.updated == 1
    assert get_paper(conn, "2026.acl-long.1")["title_zh"] == "知识图谱论文"
    assert get_paper(conn, "2026.acl-long.3")["title_zh"] == "已有翻译"
    assert report.skipped_missing_paper == 2  # acl-long.2 + journal DOI


def test_acl_volume_cache_must_cover_all_requested_years(tmp_path, monkeypatch):
    import json
    from paperbase.sources import acl

    cache = tmp_path / "volumes.json"
    cache.write_text(json.dumps([
        {"id": "2024.acl-long", "name": "ACL 2024"},
    ]), encoding="utf-8")

    def fake_soup(url):
        from bs4 import BeautifulSoup
        return BeautifulSoup(
            '<a href="/volumes/2025.acl-long">ACL 2025</a>'
            '<a href="/volumes/2025.findings-acl">Findings 2025</a>',
            "html.parser",
        )

    monkeypatch.setattr(acl, "get_bs_soup_from_url", fake_soup)
    vols = acl.get_volumes_for_years(["2024", "2025"], cache_path=cache)
    ids = {v["id"] for v in vols}
    assert "2024.acl-long" in ids
    assert "2025.acl-long" in ids
    assert "2025.findings-acl" in ids
    # Cache is refreshed and now contains all entries.
    refreshed = json.loads(cache.read_text(encoding="utf-8"))
    assert any(v["id"] == "2024.acl-long" for v in refreshed)
