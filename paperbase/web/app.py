"""FastAPI web application for the personal paper library."""
from __future__ import annotations

import html as html_mod
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from paperbase.config import load_config
from paperbase.db import (
    count_papers,
    get_paper,
    init_db,
    loads_list,
    row_to_paper,
    set_note,
    set_user_tags,
    update_paper_status,
)
from paperbase.paths import PaperPaths

def _index_html() -> str:
    return (Path(__file__).with_name("index.html")).read_text(encoding="utf-8")


def _status_widget(paper: dict) -> str:
    """Read-status controls embedded in the reader page."""
    pid = json.dumps(paper["id"], ensure_ascii=False)
    labels = {
        "new": "新收录", "in_queue": "待读队列", "reading": "在读",
        "done": "已读", "later": "稍后",
    }
    current = labels.get(paper.get("status"), paper.get("status"))
    return f"""
    <span id="paperStatus" class="status-pill">状态：{html_mod.escape(current)}</span>
    <button class="status-btn" onclick="setPaperStatus('done')">标记已读</button>
    <button class="status-btn ghost" onclick="setPaperStatus('later')">稍后再说</button>
    <script>
    const PAPER_STATUS_ID = {pid};
    async function setPaperStatus(st) {{
      try {{
        const r = await fetch('/api/papers/' + encodeURIComponent(PAPER_STATUS_ID), {{
          method: 'PATCH',
          headers: {{'Content-Type': 'application/json'}},
          body: JSON.stringify({{status: st}})
        }});
        if (!r.ok) throw new Error(await r.text());
        const d = await r.json();
        const names = {{new:'新收录', in_queue:'待读队列', reading:'在读', done:'已读', later:'稍后'}};
        const el = document.getElementById('paperStatus');
        if (el) el.textContent = '状态：' + (names[d.status] || d.status);
      }} catch (e) {{ alert('更新失败：' + e.message); }}
    }}
    </script>
    """



class QueueBody(BaseModel):
    ids: list[str]


class AskBody(BaseModel):
    question: str
    mode: str = "library"
    paper_ids: list[str] = Field(default_factory=list)


class PatchPaper(BaseModel):
    status: str | None = None
    user_tags: list[str] | None = None
    note: str | None = None


def create_app(config_path: str | None = None) -> FastAPI:
    app = FastAPI(title="PaperBase", version="0.1.0")

    def resources():
        config = load_config(config_path)
        paths = PaperPaths(config["paths"]["data_dir"])
        paths.ensure_dirs()
        conn = init_db(paths.db_path)
        try:
            yield config, paths, conn
        finally:
            conn.close()

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def index():
        return HTMLResponse(_index_html(), headers={"Cache-Control": "no-store, no-cache, must-revalidate"})

    @app.get("/digest", response_class=HTMLResponse, include_in_schema=False)
    def digest_page(res=Depends(resources)):
        _, paths, _conn = res
        digest_dir = paths.root / "digests"
        files = sorted(digest_dir.glob("*.md"), reverse=True) if digest_dir.exists() else []
        if not files:
            return "<html><body><h1>暂无早报</h1><p>先在服务器运行 <code>paper today</code></p></body></html>"
        import markdown as md

        text = files[0].read_text(encoding="utf-8")
        body = md.markdown(text, extensions=["tables", "fenced_code"])
        return (
            f"<html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1, viewport-fit=cover'>"
            f"<title>论文早报</title><style>body{{font-family:system-ui,sans-serif;max-width:900px;margin:auto;padding:1rem;line-height:1.6}}"
            f"table{{border-collapse:collapse;display:block;overflow-x:auto}}td,th{{border:1px solid #ccc;padding:.35rem}}"
            f"a{{color:#2563eb}}"
            f"@media(max-width:640px){{body{{padding:.5rem}}h1{{font-size:1.3rem}}td,th{{padding:.3rem;font-size:.85rem}}}}"
            f"</style></head><body><a href='/'>← 返回</a>{body}</body></html>"
        )

    @app.get("/api/health")
    def health(res=Depends(resources)):
        config, paths, conn = res
        import shutil as _shutil

        usage = _shutil.disk_usage(paths.root)
        return {
            "ok": True,
            "papers": count_papers(conn),
            "db": str(paths.db_path),
            "disk_used_ratio": round(usage.used / usage.total, 4),
            "disk_free_gb": round(usage.free / 1024 ** 3, 2),
        }

    @app.get("/api/digest/today")
    def digest_today(res=Depends(resources)):
        _, paths, _conn = res
        digest_dir = paths.root / "digests"
        if not digest_dir.exists():
            raise HTTPException(404, "no digest yet")
        files = sorted(digest_dir.glob("*.md"), reverse=True)
        if not files:
            raise HTTPException(404, "no digest yet")
        return {"path": str(files[0]), "content": files[0].read_text(encoding="utf-8")}

    @app.post("/api/digest/run")
    def digest_run(body: dict | None = None, res=Depends(resources)):
        config, paths, conn = res
        from paperbase.pipeline.digest import build_daily_digest

        body = body or {}
        try:
            result = build_daily_digest(
                conn, config, paths, translate=bool(body.get("translate", True)), write_file=True
            )
        except Exception as exc:
            raise HTTPException(400, f"digest generation failed: {exc}") from exc
        return result.__dict__

    @app.get("/api/papers")
    def papers(
        q: str = "",
        tag: str = "",
        year: str = "",
        source: str = "",
        venue: str = "",
        status: str = "",
        pdf_status: str = "",
        limit: int = 20,
        offset: int = 0,
        res=Depends(resources),
    ):
        _, _, conn = res
        where, params = [], []
        if q:
            where.append("(title LIKE ? OR abstract LIKE ? OR title_zh LIKE ?)")
            like = f"%{q}%"
            params += [like, like, like]
        if tag:
            where.append("(tags LIKE ? OR user_tags LIKE ?)")
            params += [f'%"{tag}"%', f'%"{tag}"%']
        if year:
            where.append("year=?")
            params.append(int(year))
        if source:
            where.append("source=?")
            params.append(source)
        if venue:
            where.append("venue LIKE ?")
            params.append(f"%{venue}%")
        if status:
            where.append("status=?")
            params.append(status)
        if pdf_status:
            where.append("pdf_status=?")
            params.append(pdf_status)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        limit = max(1, min(int(limit), 200))
        offset = max(0, int(offset))
        total = conn.execute(f"SELECT COUNT(*) FROM papers{clause}", params).fetchone()[0]
        rows = conn.execute(
            f"SELECT * FROM papers{clause} ORDER BY year DESC, venue, id LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ).fetchall()
        return {"total": total, "limit": limit, "offset": offset, "items": [row_to_paper(r) for r in rows]}

    @app.get("/api/papers/{paper_id}")
    def paper_detail(paper_id: str, res=Depends(resources)):
        _, _, conn = res
        paper = get_paper(conn, paper_id)
        if not paper:
            raise HTTPException(404, "paper not found")
        return paper

    @app.patch("/api/papers/{paper_id}")
    def patch_paper(paper_id: str, body: PatchPaper, res=Depends(resources)):
        _, _, conn = res
        if get_paper(conn, paper_id) is None:
            raise HTTPException(404, "paper not found")
        if body.status is not None:
            from paperbase.models import PaperStatus

            allowed = {e.value for e in PaperStatus}
            if body.status not in allowed:
                raise HTTPException(400, f"invalid status: {body.status}, allowed={sorted(allowed)}")
            update_paper_status(conn, paper_id, body.status)
        if body.user_tags is not None:
            set_user_tags(conn, paper_id, body.user_tags)
        if body.note is not None:
            set_note(conn, paper_id, body.note)
        return get_paper(conn, paper_id)

    @app.get("/api/queue")
    def queue_list(res=Depends(resources)):
        """Papers currently in the reading pipeline plus their task status."""
        _, _, conn = res
        papers = [
            row_to_paper(r)
            for r in conn.execute(
                "SELECT * FROM papers WHERE status IN ('in_queue','reading') "
                "ORDER BY updated_at DESC, year DESC LIMIT 200"
            ).fetchall()
        ]
        ids = [p["id"] for p in papers]
        task_rows = []
        if ids:
            placeholders = ",".join("?" for _ in ids)
            task_rows = conn.execute(
                f"SELECT paper_id, task_type, status FROM tasks WHERE paper_id IN ({placeholders})",
                ids,
            ).fetchall()
        tasks_by_paper: dict[str, dict] = {}
        for r in task_rows:
            tasks_by_paper.setdefault(r["paper_id"], {})[r["task_type"]] = r["status"]
        items = []
        for p in papers:
            p["tasks"] = tasks_by_paper.get(p["id"], {})
            items.append(p)
        return {"total": len(items), "items": items}

    @app.post("/api/queue")
    def queue(body: QueueBody, res=Depends(resources)):
        _, _, conn = res
        from paperbase.pipeline.digest import queue_papers

        queued = queue_papers(conn, body.ids)
        return {"queued": queued}

    @app.post("/api/upload")
    async def upload(
        file: UploadFile = File(...),
        paper_id: str = Form(""),
        title: str = Form(""),
        doi: str = Form(""),
        year: str = Form(""),
        venue: str = Form(""),
        authors: str = Form(""),
        tags: str = Form(""),
        issn: str = Form(""),
        res=Depends(resources),
    ):
        config, paths, conn = res
        if not (file.filename or "").lower().endswith(".pdf"):
            raise HTTPException(400, "only PDF files are accepted")
        tmp = Path(tempfile.mkdtemp(prefix="paperbase-upload-")) / "upload.pdf"
        with open(tmp, "wb") as out:
            shutil.copyfileobj(file.file, out)
        try:
            from paperbase.pipeline.pdf import ingest_uploaded_pdf

            def split_csv(value: str) -> list[str]:
                return [v.strip() for v in value.replace("；", ",").replace(";", ",").split(",") if v.strip()]

            year_int = int(year) if str(year).strip().isdigit() else None
            paper = ingest_uploaded_pdf(
                conn, paths, config, tmp,
                paper_id=paper_id.strip() or None,
                title=title.strip() or None,
                doi=doi.strip() or None,
                year=year_int,
                venue=venue.strip() or None,
                authors=split_csv(authors) or None,
                tags=split_csv(tags) or None,
                issn=issn.strip() or None,
            )
        finally:
            shutil.rmtree(tmp.parent, ignore_errors=True)
        return paper

    @app.get("/api/tags")
    def tags(res=Depends(resources)):
        _, _, conn = res
        values: list[str] = []
        for row in conn.execute("SELECT tags, user_tags FROM papers").fetchall():
            for t in loads_list(row["tags"]) + loads_list(row["user_tags"]):
                if t and t not in values:
                    values.append(t)
        return sorted(values)

    @app.get("/api/facets")
    def facets(res=Depends(resources)):
        """Dropdown facets for the paper-library search form."""
        from paperbase.pipeline.filter import TAG_NAMES

        _, _, conn = res
        tag_values: list[str] = []
        for row in conn.execute("SELECT tags, user_tags FROM papers").fetchall():
            for t in loads_list(row["tags"]) + loads_list(row["user_tags"]):
                if t and t not in tag_values:
                    tag_values.append(t)
        years = [
            r["year"] for r in conn.execute(
                "SELECT DISTINCT year FROM papers WHERE year IS NOT NULL ORDER BY year DESC"
            ).fetchall()
        ]
        source_labels = {
            "acl": "ACL Anthology",
            "openalex": "OpenAlex 期刊",
            "crossref": "Crossref",
            "arxiv": "arXiv",
            "manual": "手动上传",
        }
        sources = [
            {"value": r["source"], "label": source_labels.get(r["source"], r["source"])}
            for r in conn.execute("SELECT DISTINCT source FROM papers ORDER BY source").fetchall()
        ]
        status_labels = {
            "new": "新收录",
            "in_queue": "待读队列",
            "reading": "在读",
            "done": "已读",
            "later": "稍后",
        }
        return {
            "tags": [
                {"value": t, "label": TAG_NAMES.get(t, t)}
                for t in sorted(tag_values)
            ],
            "years": [{"value": str(y), "label": f"{y} 年"} for y in years],
            "sources": sources,
            "statuses": [
                {"value": k, "label": v} for k, v in status_labels.items()
            ],
        }

    @app.get("/api/stats")
    def stats(res=Depends(resources)):
        _, _, conn = res
        out: dict[str, Any] = {"papers": count_papers(conn)}
        for key, sql in {
            "by_source": "SELECT source AS k, COUNT(*) AS n FROM papers GROUP BY source ORDER BY n DESC",
            "by_year": "SELECT year AS k, COUNT(*) AS n FROM papers GROUP BY year ORDER BY year DESC",
            "by_status": "SELECT status AS k, COUNT(*) AS n FROM papers GROUP BY status",
            "by_pdf_status": "SELECT pdf_status AS k, COUNT(*) AS n FROM papers GROUP BY pdf_status",
            "by_parse_status": "SELECT parse_status AS k, COUNT(*) AS n FROM papers GROUP BY parse_status",
        }.items():
            out[key] = {r["k"]: r["n"] for r in conn.execute(sql).fetchall()}
        tags: dict[str, int] = {}
        for row in conn.execute("SELECT tags FROM papers").fetchall():
            for t in loads_list(row["tags"]):
                tags[t] = tags.get(t, 0) + 1
        out["by_tag"] = dict(sorted(tags.items(), key=lambda kv: -kv[1]))
        return out

    @app.post("/api/ask")
    def ask(body: AskBody, res=Depends(resources)):
        config, paths, conn = res
        from paperbase.dci.agent import DCIQAAgent

        if not body.question.strip():
            raise HTTPException(400, "question is required")
        answer = DCIQAAgent(conn, config, paths).ask(
            body.question.strip(),
            mode=body.mode if body.mode in ("paper", "library", "compare") else "library",
            paper_ids=body.paper_ids,
        )
        return answer.__dict__

    @app.get("/reader/{paper_id}", response_class=HTMLResponse)
    def reader(paper_id: str, lang: str = "en", raw: bool = False, res=Depends(resources)):
        _, paths, conn = res
        paper = get_paper(conn, paper_id)
        if not paper:
            raise HTTPException(404, "paper not found")
        if paper.get("status") in ("new", "in_queue", "later"):
            update_paper_status(conn, paper_id, "reading")
            paper = get_paper(conn, paper_id)

        path_value = paper.get("md_zh_path") if lang == "zh" else paper.get("md_path")
        if not path_value or not Path(path_value).exists():
            raise HTTPException(404, "markdown not available")
        import markdown as md

        text = Path(path_value).read_text(encoding="utf-8")
        if raw:
            lines = text.splitlines()
            body = "\n".join(
                f'<span id="L{i+1}">{i+1}: {html_mod.escape(line)}</span>'
                for i, line in enumerate(lines)
            )
            return (
                f"<html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1, viewport-fit=cover'>"
                f"<title>{html_mod.escape(paper['title'])}</title><style>"
                f"body{{font-family:monospace;max-width:1000px;margin:auto;padding:1rem;font-size:14px}}"
                f"pre{{white-space:pre-wrap;overflow-x:auto}}span{{display:block;min-height:1.1em}}"
                f"span:target{{background:#fef08a}}"
                f".status-pill{{display:inline-block;background:#eef2ff;color:#3730a3;border-radius:99px;padding:.2rem .7rem;font-size:.85rem;margin:.3rem .5rem .3rem 0}}"
                f".status-btn{{border:1px solid #2563eb;background:#2563eb;color:#fff;border-radius:8px;padding:.35rem .7rem;font-size:.9rem;margin-right:.4rem;cursor:pointer}}"
                f".status-btn.ghost{{background:#fff;color:#2563eb}}"
                f"@media(max-width:640px){{body{{padding:.5rem;font-size:12px}}}}"
                f"</style></head><body><p><a href='/'>← 返回</a> | <a href='?raw=0&lang={lang}'>渲染版</a></p>"
                f"<h1>{html_mod.escape(paper['title'])}</h1><p>{_status_widget(paper)}</p><pre>{body}</pre></body></html>"
            )
        body_html = md.markdown(text, extensions=["tables", "fenced_code"])
        paper_id_json = json.dumps(paper["id"], ensure_ascii=False)
        ask_panel = f"""
        <details id="ask" class="ask-panel">
          <summary>🤖 问这篇论文</summary>
          <textarea id="question" rows="3" placeholder="例如：这篇论文的核心方法是什么？实验用了哪些数据集？"></textarea>
          <button class="ask-btn" onclick="askThisPaper()">提问</button>
          <pre id="answer" class="light" style="display:none"></pre>
          <div id="citations" class="cites"></div>
        </details>
        <script>
        const PAPER_ID = {paper_id_json};
        function citeLinks(list) {{
          return (list || []).map(c => {{
            const m = c.match(/^\\[([^:\\]]+):([0-9]+(?:-[0-9]+)?)\\]$/);
            if (!m) return '<span>' + c + '</span>';
            const p = m[1];
            const zh = p.endsWith('.zh.md');
            const id = p.replace(/\\.(zh\\.)?md$/, '');
            const base = id.split('/').pop() || PAPER_ID;
            const line = m[2].split('-')[0];
            return `<a href="/reader/${{encodeURIComponent(base)}}?raw=1&lang=${{zh?'zh':'en'}}#L${{line}}" target="_blank">${{c}}</a>`;
          }}).join(' ') || '<span class="muted">无结构化引用</span>';
        }}
        async function askThisPaper() {{
          const q = document.getElementById('question').value.trim();
          if (!q) {{ alert('请输入问题'); return; }}
          const box = document.getElementById('answer');
          box.style.display = 'block';
          box.textContent = '检索中…';
          document.getElementById('citations').innerHTML = '';
          try {{
            const r = await fetch('/api/ask', {{
              method: 'POST',
              headers: {{'Content-Type': 'application/json'}},
              body: JSON.stringify({{question: q, mode: 'paper', paper_ids: [PAPER_ID]}})
            }});
            if (!r.ok) throw new Error(await r.text());
            const d = await r.json();
            box.textContent = d.answer + `\n\nConfidence: ${{d.confidence}} · 工具调用: ${{d.tool_calls}} · tokens: ${{(d.prompt_tokens||0)+(d.completion_tokens||0)}}`;
            document.getElementById('citations').innerHTML = citeLinks(d.citations);
          }} catch (e) {{
            box.textContent = '提问失败：' + e.message;
          }}
        }}
        if (new URLSearchParams(location.search).get('ask') === '1' || location.hash === '#ask') {{
          const panel = document.getElementById('ask');
          if (panel) {{ panel.open = true; panel.scrollIntoView({{behavior:'smooth'}}); }}
        }}
        </script>"""
        return (
            f"<html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1, viewport-fit=cover'>"
            f"<title>{html_mod.escape(paper['title'])}</title><style>body{{font-family:serif;max-width:960px;margin:auto;padding:1rem;line-height:1.7}}"
            f"pre{{white-space:pre-wrap;overflow-x:auto}}table{{border-collapse:collapse;display:block;overflow-x:auto}}td,th{{border:1px solid #ccc;padding:.3rem}}"
            f"img{{max-width:100%}}"
            f".ask-panel{{border:1px solid #c7d2fe;border-radius:12px;padding:.6rem .8rem;background:#f8faff;margin:0 0 1rem}}"
            f".status-pill{{display:inline-block;background:#eef2ff;color:#3730a3;border-radius:99px;padding:.2rem .7rem;font-size:.85rem;margin:.3rem .5rem .3rem 0}}"
            f".status-btn{{border:1px solid #2563eb;background:#2563eb;color:#fff;border-radius:8px;padding:.35rem .7rem;font-size:.9rem;margin-right:.4rem;cursor:pointer}}"
            f".status-btn.ghost{{background:#fff;color:#2563eb}}"
            f".ask-panel summary{{font-weight:700;font-size:1rem;cursor:pointer;min-height:44px;display:flex;align-items:center}}"
            f".ask-panel textarea{{width:100%;font-size:16px;padding:.6rem;border:1px solid #cbd5e1;border-radius:8px;margin:.5rem 0}}"
            f".ask-panel .ask-btn{{background:#2563eb;color:#fff;border:0;padding:.6rem 1rem;border-radius:8px;font-size:16px;min-height:44px}}"
            f".ask-panel pre.light{{background:#f8fafc;border:1px solid #e2e8f0;white-space:pre-wrap;padding:.6rem;border-radius:8px;font-size:.85rem}}"
            f".cites a{{display:inline-block;color:#2563eb;text-decoration:none;margin:.15rem .4rem .15rem 0;font-size:.85rem}}"
            f"@media(max-width:640px){{body{{padding:.5rem;font-size:15px}}h1{{font-size:1.3rem}}td,th{{padding:.25rem;font-size:.8rem}}.ask-panel{{padding:.5rem}}}}"
            f"</style></head><body><p><a href='/'>← 返回</a> | <a href='?lang=zh'>中文</a> | <a href='?lang=en'>English</a> | <a href='?raw=1'>原文行号</a></p>"
            f"<h1>{html_mod.escape(paper['title'])}</h1><p>{_status_widget(paper)}</p>{ask_panel}{body_html}</body></html>"
        )

    return app


app = create_app()


def main() -> None:
    import uvicorn

    config = load_config(os.environ.get("PAPERBASE_CONFIG"))
    access = config.get("access", {})
    uvicorn.run(
        "paperbase.web.app:app",
        host=access.get("bind_host", "127.0.0.1"),
        port=int(access.get("bind_port", 8000)),
        workers=1,
    )


if __name__ == "__main__":
    main()
