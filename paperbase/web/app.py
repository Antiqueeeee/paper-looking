"""FastAPI web application for the personal paper library."""
from __future__ import annotations

import html as html_mod
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, File, HTTPException, UploadFile
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

INDEX_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PaperBase</title>
<style>
 body{font-family:system-ui,sans-serif;margin:1rem;line-height:1.5;background:#fafafa;color:#222}
 h1{font-size:1.4rem} section{background:#fff;border:1px solid #ddd;border-radius:8px;padding:1rem;margin:1rem 0}
 input,textarea,button{font:inherit;padding:.5rem;margin:.2rem 0;width:100%;box-sizing:border-box}
 button{background:#2563eb;color:#fff;border:0;border-radius:6px;cursor:pointer}
 pre{white-space:pre-wrap;background:#f4f4f4;padding:.6rem;border-radius:6px}
 .row{display:flex;gap:.5rem} .row>*{flex:1}
 a{color:#2563eb}
</style>
</head>
<body>
<h1>PaperBase 个人论文库</h1>
<section><h2>每日早报</h2><button onclick="loadDigest()">加载今日早报</button><pre id="digest">点击加载。</pre></section>
<section><h2>勾选论文</h2>
<input id="qids" placeholder="论文 ID，多个用空格分隔"><br>
<button onclick="queuePapers()">加入阅读队列</button></section>
<section><h2>搜索</h2>
<div class="row"><input id="q" placeholder="标题/摘要关键词"><input id="tag" placeholder="标签 kg/rag/kbqa"></div>
<button onclick="searchPapers()">搜索</button><pre id="results"></pre></section>
<section><h2>上传 PDF</h2><input type="file" id="pdf" accept="application/pdf"><button onclick="uploadPdf()">上传</button><pre id="uploadResult"></pre></section>
<section><h2>问 AI</h2>
<input id="askPaper" placeholder="单篇论文 ID（可留空=全库）">
<textarea id="question" rows="3" placeholder="例如：这篇论文的核心方法是什么？"></textarea>
<button onclick="ask()">提问</button><pre id="answer"></pre></section>
<section><h2>接口</h2><p><a href="/docs">API 文档</a> · <a href="/api/stats">统计</a> · <a href="/api/health">健康检查</a></p></section>
<script>
async function j(url, opts={}){const r=await fetch(url,opts);const d=await r.json();return d}
function loadDigest(){j('/api/digest/today').then(d=>document.getElementById('digest').textContent=d.content).catch(e=>document.getElementById('digest').textContent='暂无早报')}
function queuePapers(){const ids=document.getElementById('qids').value.split(/\\s+/).filter(Boolean);j('/api/queue',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({ids})}).then(d=>alert('已加入：'+d.queued))}
function searchPapers(){const q=document.getElementById('q').value;const tag=document.getElementById('tag').value;j('/api/papers?q='+encodeURIComponent(q)+'&tag='+encodeURIComponent(tag)+'&limit=20').then(d=>{document.getElementById('results').textContent=JSON.stringify(d.items,null,2)})}
async function uploadPdf(){const f=document.getElementById('pdf').files[0];if(!f)return;const fd=new FormData();fd.append('file',f);const r=await fetch('/api/upload',{method:'POST',body:fd});document.getElementById('uploadResult').textContent=JSON.stringify(await r.json(),null,2)}
function ask(){const body={question:document.getElementById('question').value,mode:'library'};const pid=document.getElementById('askPaper').value.trim();if(pid){body.mode='paper';body.paper_ids=[pid]}j('/api/ask',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}).then(d=>{document.getElementById('answer').textContent=d.answer+'\\n\\n'+JSON.stringify(d.citations)})}
</script>
</body></html>"""


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
        return INDEX_HTML

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

    @app.post("/api/queue")
    def queue(body: QueueBody, res=Depends(resources)):
        _, _, conn = res
        from paperbase.pipeline.digest import queue_papers

        queued = queue_papers(conn, body.ids)
        return {"queued": queued}

    @app.post("/api/upload")
    async def upload(file: UploadFile = File(...), paper_id: str = "", title: str = "", res=Depends(resources)):
        config, paths, conn = res
        if not (file.filename or "").lower().endswith(".pdf"):
            raise HTTPException(400, "only PDF files are accepted")
        tmp = Path(tempfile.mkdtemp(prefix="paperbase-upload-")) / "upload.pdf"
        with open(tmp, "wb") as out:
            shutil.copyfileobj(file.file, out)
        try:
            from paperbase.pipeline.pdf import ingest_uploaded_pdf

            paper = ingest_uploaded_pdf(
                conn, paths, config, tmp,
                paper_id=paper_id or None,
                title=title or None,
            )
        finally:
            shutil.rmtree(tmp.parent, ignore_errors=True)
        return paper

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
    def reader(paper_id: str, lang: str = "en", res=Depends(resources)):
        _, paths, conn = res
        paper = get_paper(conn, paper_id)
        if not paper:
            raise HTTPException(404, "paper not found")
        path_value = paper.get("md_zh_path") if lang == "zh" else paper.get("md_path")
        if not path_value or not Path(path_value).exists():
            raise HTTPException(404, "markdown not available")
        import markdown as md

        text = Path(path_value).read_text(encoding="utf-8")
        body_html = md.markdown(text, extensions=["tables", "fenced_code"])
        return (
            f"<html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width'>"
            f"<title>{html_mod.escape(paper['title'])}</title><style>body{{font-family:serif;max-width:960px;margin:auto;padding:1rem}}"
            f"pre{{white-space:pre-wrap}}table{{border-collapse:collapse}}td,th{{border:1px solid #ccc;padding:.3rem}}"
            f"</style></head><body><p><a href='/'>← 返回</a> | <a href='?lang=zh'>中文</a> | <a href='?lang=en'>English</a></p>"
            f"<h1>{html_mod.escape(paper['title'])}</h1>{body_html}</body></html>"
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
