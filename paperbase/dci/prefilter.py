"""Metadata prefilter: narrow the DCI question scope through SQLite before grep.

This is intentionally rule-based and cheap. It only restricts the file scope;
the agent still verifies content with rg/read_file.
"""
from __future__ import annotations

import re

from paperbase.db import loads_list

YEAR_RE = re.compile(r"\b(20\d{2})\b")

TAG_HINTS = {
    "rag": ["graphrag", "rag", "retrieval-augmented", "retrieval augmented", "检索增强"],
    "kg": ["knowledge graph", "knowledge graphs", "知识图谱", " kg ", "kgqa"],
    "kbqa": ["kbqa", "question answering over knowledge", "知识库问答"],
    "ie": ["information extraction", "relation extraction", "event extraction", "信息抽取"],
    "mem": ["agent memory", "memory management", "agent 记忆", "记忆管理"],
    "ds": ["synthetic data", "data synthesis", "数据合成"],
}

VENUE_HINTS = [
    "acl", "emnlp", "naacl", "eacl", "findings", "tacl", "lrec", "coling",
    "tkde", "tois", "kbs", "ipm", "is", "eswa", "vldb", "tods", "jair",
    "www", "csl", "jws", "kais", "tkdd", "dke", "dmkd", "tist", "jasis",
    "ijswis", "nle", "neurocomputing", "machine learning", "information sciences",
]


def extract_filters(question: str) -> tuple[int | None, list[str], list[str]]:
    q = question.lower()
    year = None
    m = YEAR_RE.search(q)
    if m:
        year = int(m.group(1))
    venue_hits = [v for v in VENUE_HINTS if v in q]
    tag_hits = [tag for tag, words in TAG_HINTS.items() if any(w in q for w in words)]
    return year, venue_hits, tag_hits


def prefilter_parsed_papers(conn, question: str, limit: int = 200) -> list[dict]:
    """Return parsed-paper dicts relevant to the question's explicit constraints."""
    year, venue_hits, tag_hits = extract_filters(question)
    rows = conn.execute(
        """
        SELECT id, title, title_zh, year, venue, tags, md_path, md_zh_path
          FROM papers
         WHERE parse_status='done' AND md_path != ''
         ORDER BY year DESC, venue, id
        """
    ).fetchall()

    out: list[dict] = []
    for row in rows:
        d = dict(row)
        d["tags"] = loads_list(row["tags"])
        if year is not None and d.get("year") != year:
            continue
        if venue_hits:
            venue_l = (d.get("venue") or "").lower()
            if not any(v in venue_l for v in venue_hits):
                continue
        if tag_hits and not any(t in d["tags"] for t in tag_hits):
            continue
        out.append(d)
        if len(out) >= limit:
            break
    return out


__all__ = ["extract_filters", "prefilter_parsed_papers"]
