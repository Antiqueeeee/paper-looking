"""Phase 6 tests: FastAPI endpoints."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from paperbase.db import init_db, upsert_paper
from paperbase.paths import PaperPaths


@pytest.fixture()
def client(tmp_path):
    data_dir = tmp_path / "data"
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(f'[paths]\ndata_dir = "{data_dir}"\n', encoding="utf-8")
    # Pre-populate DB used by the app.
    paths = PaperPaths(data_dir)
    paths.ensure_dirs()
    conn = init_db(paths.db_path)
    upsert_paper(conn, {
        "id": "2026.acl-long.1", "source": "acl", "title": "GraphRAG for KBQA",
        "abstract": "retrieval augmented generation", "year": 2026, "venue": "acl-long",
        "url": "https://x/1", "pdf_url": "https://x/1.pdf", "tags": ["rag"],
    })
    conn.close()

    from paperbase.web.app import create_app
    app = create_app(str(cfg_path))
    with TestClient(app) as c:
        c.cfg_path = str(cfg_path)
        yield c


def test_index_and_digest_pages(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "今日早报" in r.text
    assert "论文库" in r.text
    assert "全部标签" in r.text
    assert "全部年份" in r.text
    assert "全部来源" in r.text
    assert "阅读 &amp; 提问" in r.text
    r = client.get("/digest")
    assert r.status_code in (200, 404)  # digest may or may not exist yet
    r = client.get("/api/tags")
    assert r.status_code == 200
    assert "rag" in r.json()
    r = client.get("/api/facets")
    assert r.status_code == 200
    body = r.json()
    assert {"value": "rag", "label": "RAG"} in body["tags"]
    assert {"value": "2026", "label": "2026 年"} in body["years"]


def test_health_and_stats(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["papers"] == 1
    r = client.get("/api/stats")
    assert r.status_code == 200
    assert r.json()["by_tag"]["rag"] == 1


def test_paper_search_and_detail(client):
    r = client.get("/api/papers", params={"q": "GraphRAG", "tag": "rag"})
    assert r.status_code == 200
    assert r.json()["total"] == 1
    assert r.json()["items"][0]["id"] == "2026.acl-long.1"

    r = client.get("/api/papers/2026.acl-long.1")
    assert r.json()["title"] == "GraphRAG for KBQA"


def test_queue_and_ask_without_parsed(client):
    r = client.post("/api/queue", json={"ids": ["2026.acl-long.1"]})
    assert r.status_code == 200
    assert r.json()["queued"] == 1
    # No parsed markdown: answer should be honest and must not require LLM key.
    r = client.post("/api/ask", json={"question": "方法是什么？", "mode": "paper", "paper_ids": ["2026.acl-long.1"]})
    assert r.status_code == 200
    assert r.json()["confidence"] == 0.0
    assert "尚未解析" in r.json()["answer"]


def test_upload_and_reader_flow(client, tmp_path):
    pdf = tmp_path / "x.pdf"
    pdf.write_bytes(b"%PDF-1.4\n" + b"x" * 300)
    r = client.post(
        "/api/upload",
        files={"file": ("x.pdf", open(pdf, "rb"), "application/pdf")},
        data={"title": "Manual Paper"},
    )
    assert r.status_code == 200
    paper_id = r.json()["id"]
    assert paper_id.startswith("manual:")
    # Reader should 404 before parsing, not 500.
    r = client.get(f"/reader/{paper_id}")
    assert r.status_code == 404

    # Once markdown exists, raw mode exposes stable line anchors for citations.
    from paperbase.config import load_config
    from paperbase.paths import PaperPaths as PP
    from paperbase.db import init_db as init_db2, set_local_file as set_local_file2
    from paperbase.pipeline.mineru import write_markdown_atomic

    cfg = load_config(client.cfg_path)
    paths2 = PP(cfg["paths"]["data_dir"])
    conn2 = init_db2(paths2.db_path)
    md_path = paths2.paper_md(conn2.execute("SELECT * FROM papers WHERE id=?", (paper_id,)).fetchone())
    write_markdown_atomic(md_path, "line one\nline two\n")
    set_local_file2(conn2, paper_id, md_path=str(md_path))
    conn2.close()
    r = client.get(f"/reader/{paper_id}?raw=1")
    assert r.status_code == 200
    assert 'id="L1"' in r.text

    r = client.get(f"/reader/{paper_id}")
    assert r.status_code == 200
    assert "问这篇论文" in r.text
    assert f"PAPER_ID = \"{paper_id}\"" in r.text
