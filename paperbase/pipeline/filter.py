"""Interest rules for paper titles and abstracts.

Rules are regular expressions evaluated case-insensitively. System tags are
stored in `papers.tags`; user-owned tags stay in `papers.user_tags`.
"""
from __future__ import annotations

import re
from functools import lru_cache
from typing import Iterable

from paperbase.db import dumps_json, loads_list, utcnow

TAG_NAMES = {
    "kg": "知识图谱",
    "ie": "信息抽取",
    "kbqa": "知识库问答",
    "sp": "语义解析",
    "ds": "数据合成",
    "rag": "RAG",
    "mrag": "多模态RAG",
    "mem": "Agent记忆",
}

DEFAULT_RULES: dict[str, list[str]] = {
    "kg": [
        r"\bknowledge\s*-?\s*graphs?\b",
        r"\bknowledge\s*-?\s*bases?\b",
        r"\bkg\s*embedding",
        r"\bknowledge\s+embedding",
        r"\bentity\s+alignment\b",
        r"\bgraph\s+retrieval[- ]augmented",
        r"\bgraphrag\b",
        r"\bkg\s*[-]?rag\b",
        r"\bknowledge\s+graph\s+(completion|construction|augmented|augmentation|enhanced|reasoning|alignment|grounded)\b",
        r"\bontolog(y|ies|ical)\b",
        r"\blink\s+prediction\b",
    ],
    "ie": [
        r"\binformation\s+extraction\b",
        r"\brelation\s+extraction\b",
        r"\bevent\s+extraction\b",
        r"\bnamed\s+entity\s+recognition\b",
        r"\bner\b",
        r"\bentity\s+(linking|disambiguation|resolution)\b",
        r"\bopen\s+information\s+extraction\b",
        r"\bopenie\b",
        r"\btriple\s+extraction\b",
        r"\bjoint\s+extraction\b",
        r"\bargument\s+extraction\b",
        r"\btemplate\s+filling\b",
        r"\bdocument\s+level\s+relation",
        r"\bzero[-\s]?shot\s+relation",
        r"\bentity\s+recognition\b",
    ],
    "kbqa": [
        r"\bkbqa\b",
        r"\bkgqa\b",
        r"\bknowledge\s*-?\s*base\s+question\s+answering\b",
        r"\bknowledge\s+graph\s+question\s+answering\b",
        r"\bquestion\s+answering\s+over\s+(knowledge\s+graphs?\b|knowledge\s+bases?\b)",
        r"\bqa\s+over\s+(knowledge\s+graphs?\b|knowledge\s+bases?\b)",
    ],
    "sp": [
        r"\bsemantic\s+parsing\b",
        r"\bsemantic\s+parser(s)?\b",
        r"\blogical\s+forms?\b",
    ],
    "ds": [
        r"\bsynthetic\s+data\b",
        r"\bdata\s+synthesis\b",
        r"\bdata\s+generation\b",
        r"\bdata\s+augmentation\b",
        r"\bsynthesi[sz]e\s+(training\s+)?data\b",
        r"\binstruction\s+(synthesis|generation)\b",
        r"\bself[-\s]?instruction\b",
        r"\b(question|conversation|dialogue|demonstration)\s+generation\b",
        r"\bdata\s+flywheel\b",
        r"\b(seed|diverse|quality)\s+data\s+generation\b",
    ],
    "rag": [
        r"\bretrieval[- ]augmented\b",
        r"\brag\b",
        r"\bgraphrag\b",
        r"\brerank(ing|er|ed)?\b",
        r"\bretrieval[- ]based\s+generation\b",
    ],
    "mrag": [
        r"\bmultimodal\s+retrieval[- ]augmented\b",
        r"\bmultimodal\s+rag\b",
        r"\bmm[- ]?rag\b",
        r"\bmultimodal\s+retrieval\b",
        r"\bvision[- ]language\s+retrieval[- ]augmented\b",
        r"\b(visual|video|image|audio)\s+retrieval[- ]augmented\b",
        r"\bimage[- ]text\s+retrieval[- ]augmented\b",
    ],
    "mem": [
        r"\bagents?\s+memory\b",
        r"\bmemory[- ]augmented\s+agents?\b",
        r"\bmemory[- ]enabled\s+agents?\b",
        r"\bmemory\s+(management|design|consolidation|compaction|reflection|organization|hierarchy|architecture|mechanism|bank|retrieval|editing)\b",
        r"\b(episodic|semantic|working|long[- ]term|conversational?)\s+memory\b",
        r"\bllm\s+memory\b",
    ],
}


@lru_cache(maxsize=1)
def build_matchers(rules_json: str | None = None) -> dict[str, list[re.Pattern]]:
    rules = DEFAULT_RULES
    if rules_json:
        import json
        try:
            parsed = json.loads(rules_json)
            if isinstance(parsed, dict) and parsed:
                rules = parsed
        except json.JSONDecodeError:
            pass
    return {tag: [re.compile(p, re.IGNORECASE) for p in pats] for tag, pats in rules.items()}


def match_text(title: str, abstract: str = "", matchers: dict | None = None) -> list[str]:
    matchers = matchers or build_matchers()
    text = f"{title}\n{abstract}"
    tags: list[str] = []
    for tag, patterns in matchers.items():
        if any(p.search(text) for p in patterns):
            tags.append(tag)
    return tags


def match_paper(paper: dict, matchers: dict | None = None) -> list[str]:
    return match_text(
        str(paper.get("title") or ""),
        str(paper.get("abstract") or ""),
        matchers,
    )


def apply_rules(conn, paper_ids: Iterable[str] | None = None, matchers: dict | None = None) -> int:
    """Recompute system tags for papers and persist changed rows.

    Returns number of rows whose tags changed. `paper_ids=None` means all.
    """
    matchers = matchers or build_matchers()
    query = "SELECT id, title, abstract, tags FROM papers"
    params: tuple = ()
    if paper_ids is not None:
        ids = list(paper_ids)
        if not ids:
            return 0
        placeholders = ",".join("?" for _ in ids)
        query += f" WHERE id IN ({placeholders})"
        params = tuple(ids)

    changed = 0
    rows = conn.execute(query, params).fetchall()
    for row in rows:
        new_tags = match_text(row["title"], row["abstract"], matchers)
        old_tags = loads_list(row["tags"])
        if new_tags != old_tags:
            conn.execute(
                "UPDATE papers SET tags=?, updated_at=? WHERE id=?",
                (dumps_json(new_tags), utcnow(), row["id"]),
            )
            changed += 1
    conn.commit()
    return changed


__all__ = ["TAG_NAMES", "DEFAULT_RULES", "build_matchers", "match_text", "match_paper", "apply_rules"]
