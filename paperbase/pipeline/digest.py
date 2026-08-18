"""Daily digest generation and reading queue."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from paperbase.db import get_meta, loads_list, set_meta, utcnow
from paperbase.pipeline.filter import TAG_NAMES, apply_rules

TAG_ORDER = ["kg", "ie", "kbqa", "sp", "ds", "rag", "mrag", "mem"]
DIGEST_BASELINE_KEY = "digest:last_run"


@dataclass
class DigestResult:
    date: str
    path: str = ""
    new_total: int = 0
    matched: int = 0
    translated: int = 0
    cached: int = 0
    baseline: bool = False
    message: str = ""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _primary_tag(tags: list[str]) -> str:
    for tag in TAG_ORDER:
        if tag in tags:
            return tag
    return tags[0] if tags else "other"


def ensure_digest_baseline(conn) -> tuple[bool, str]:
    """Return (is_first_run, since). First run sets a baseline and digests nothing."""
    since = get_meta(conn, DIGEST_BASELINE_KEY, "")
    if not since:
        since = _now()
        set_meta(conn, DIGEST_BASELINE_KEY, since)
        return True, since
    return False, since


def new_matched_papers(conn, since: str, limit: int = 500) -> list[dict]:
    rows = conn.execute(
        """
        SELECT id, title, title_zh, abstract, abstract_zh, authors, year, venue,
               url, pdf_url, pdf_status, tags, source
          FROM papers
         WHERE created_at >= ? AND tags != '[]'
         ORDER BY year DESC, venue, id
         LIMIT ?
        """,
        (since, limit),
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["tags"] = loads_list(r["tags"])
        out.append(d)
    return out


def build_daily_digest(
    conn,
    config: dict,
    paths,
    *,
    client=None,
    translate: bool = True,
    limit: int = 500,
    write_file: bool = True,
) -> DigestResult:
    """Apply rules, translate newly matched metadata and write today's digest."""
    first, since = ensure_digest_baseline(conn)
    today = datetime.now(timezone.utc).date().isoformat()
    result = DigestResult(date=today, baseline=first)

    if first:
        result.message = "首次运行：已建立时间基线，今日不生成历史早报。"
        return result

    # Only new rows since the previous digest are matched, never the whole corpus.
    new_rows = conn.execute(
        "SELECT id FROM papers WHERE created_at >= ?",
        (since,),
    ).fetchall()
    apply_rules(conn, [r["id"] for r in new_rows])
    if config.get("interest"):
        from paperbase.interest import classify_database

        classify_database(conn, config, paper_ids=[r["id"] for r in new_rows])
    matched = new_matched_papers(conn, since, limit=limit)
    result.new_total = len(matched)
    result.matched = len(matched)

    if matched and translate:
        if client is None:
            from paperbase.pipeline.translate import make_llm_client

            client = make_llm_client(config, conn)
        from paperbase.pipeline.translate import translate_meta_for_papers

        stats = translate_meta_for_papers(conn, client, config, [p["id"] for p in matched])
        result.translated = stats.translated
        result.cached = stats.cached
        # Re-read rows so title_zh is present in the rendered digest.
        matched = new_matched_papers(conn, since, limit=limit)

    md = render_digest(matched, result)
    if write_file:
        out_path = Path(paths.root) / "digests" / f"{today}.md"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = out_path.with_suffix(".md.part")
        tmp.write_text(md, encoding="utf-8")
        tmp.replace(out_path)
        result.path = str(out_path)
        set_meta(conn, DIGEST_BASELINE_KEY, _now())
    result.message = (
        f"新增 {len(matched)} 篇相关论文，翻译 {result.translated} 篇，缓存 {result.cached} 篇。"
    )
    return result


def render_digest(papers: list[dict], result: DigestResult | None = None) -> str:
    lines = [
        "# 论文早报",
        "",
        f"- 日期：{datetime.now(timezone.utc).date().isoformat()}",
        f"- 新增相关论文：{len(papers)} 篇",
    ]
    if result:
        lines.append(f"- 本次翻译：{result.translated} 篇 / 缓存：{result.cached} 篇")
    lines.append("")

    if not papers:
        lines.append("今日无新增相关论文。")
        return "\n".join(lines) + "\n"

    groups: dict[str, list[dict]] = {}
    for p in papers:
        groups.setdefault(_primary_tag(p["tags"]), []).append(p)

    for tag in TAG_ORDER:
        group = groups.get(tag)
        if not group:
            continue
        lines.append(f"## {TAG_NAMES.get(tag, tag)}（{len(group)} 篇）")
        lines.append("")
        for p in group:
            title_zh = p.get("title_zh") or "（翻译待生成）"
            pdf_state = {"downloaded": "✅", "needs_upload": "⬜", "none": "📎"}.get(p.get("pdf_status"), "📎")
            extra_tags = [TAG_NAMES.get(t, t) for t in p["tags"] if t != tag]
            extra = f"  [{'/'.join(extra_tags)}]" if extra_tags else ""
            link = p.get("url") or p.get("pdf_url") or p.get("id")
            authors = ", ".join((p.get("authors") or [])[:3])
            lines.append(f"1. [{p.get('year')}] [{p.get('venue') or '-'}] {p['title']}")
            lines.append(f"   中文：{title_zh}{extra}")
            if authors:
                lines.append(f"   作者：{authors}")
            lines.append(f"   来源：{link}  |  PDF：{pdf_state}")
            lines.append("")
    return "\n".join(lines) + "\n"


def queue_papers(conn, paper_ids: Iterable[str]) -> int:
    """Mark papers as in_queue and create the first PDF pipeline tasks."""
    ids = [str(i) for i in paper_ids if str(i).strip()]
    if not ids:
        return 0
    placeholders = ",".join("?" for _ in ids)
    conn.execute(
        f"UPDATE papers SET status='in_queue', updated_at=? WHERE id IN ({placeholders})",
        (utcnow(), *ids),
    )
    conn.commit()

    from paperbase.db import get_paper
    from paperbase.pipeline.pdf import create_pdf_pipeline_tasks

    queued = 0
    for pid in ids:
        paper = get_paper(conn, pid)
        if paper and paper["status"] == "in_queue":
            create_pdf_pipeline_tasks(conn, paper)
            queued += 1
    return queued


__all__ = [
    "TAG_ORDER",
    "DigestResult",
    "ensure_digest_baseline",
    "new_matched_papers",
    "build_daily_digest",
    "render_digest",
    "queue_papers",
]
