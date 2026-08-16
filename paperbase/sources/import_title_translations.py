"""Import legacy Chinese title translations left by the previous pipeline.

Sources:
  * titles_zh/zh_*.jsonl            id -> zh (ACL 2026 interest titles)
  * interests_2026.zh.jsonl         id -> zh (same ACL batch)
  * interest_titles_2025_2026.txt   "English / Chinese (DOI)" journal titles

Only rows whose `title_zh` is still empty are updated, so newer translations
produced by the current pipeline are never overwritten.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from paperbase.db import utcnow

JOURNAL_LINE_RE = re.compile(
    r"^\[\d{4}\]\s+\[[^\]]+\]\s+(.+?)\s+/\s+(.+?)\s+\((10\.[^)]+)\)"
)


@dataclass
class TitleImportReport:
    found: int = 0
    updated: int = 0
    skipped_existing: int = 0
    skipped_missing_paper: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "found": self.found,
            "updated": self.updated,
            "skipped_existing": self.skipped_existing,
            "skipped_missing_paper": self.skipped_missing_paper,
            "errors": len(self.errors),
        }


def _merge(pairs: dict[str, str], items) -> None:
    for key, zh in items:
        if key and zh and key not in pairs:
            pairs[key] = zh


def load_legacy_translations(legacy_dir: str | Path) -> dict[str, str]:
    """Return {paper_id_or_doi_lower: chinese_title} from legacy files."""
    root = Path(legacy_dir)
    pairs: dict[str, str] = {}

    for path in sorted(root.glob("titles_zh/zh_*.jsonl")):
        try:
            with open(path, encoding="utf-8") as f:
                _merge(pairs, (
                    (str(row.get("id", "")).strip(), str(row.get("zh", "")).strip())
                    for line in f
                    if line.strip()
                    for row in [json.loads(line)]
                ))
        except (OSError, json.JSONDecodeError):
            continue

    for path in sorted(root.glob("interests_*.zh.jsonl")):
        try:
            with open(path, encoding="utf-8") as f:
                _merge(pairs, (
                    (str(row.get("id", "")).strip(), str(row.get("zh", "")).strip())
                    for line in f
                    if line.strip()
                    for row in [json.loads(line)]
                ))
        except (OSError, json.JSONDecodeError):
            continue

    for path in sorted(root.glob("interest_titles_*.txt")):
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    m = JOURNAL_LINE_RE.match(line.strip())
                    if m:
                        doi = m.group(3).strip().lower()
                        zh = m.group(2).strip().rstrip("[").strip()
                        _merge(pairs, [(doi, zh)])
        except OSError:
            continue

    return pairs


def import_title_translations(conn, legacy_dir: str | Path) -> TitleImportReport:
    pairs = load_legacy_translations(legacy_dir)
    report = TitleImportReport(found=len(pairs))
    for key, zh in pairs.items():
        row = conn.execute("SELECT id, title_zh FROM papers WHERE id=?", (key,)).fetchone()
        if row is None:
            report.skipped_missing_paper += 1
            continue
        if (row["title_zh"] or "").strip():
            report.skipped_existing += 1
            continue
        conn.execute(
            "UPDATE papers SET title_zh=?, updated_at=? WHERE id=?",
            (zh, utcnow(), key),
        )
        report.updated += 1
    conn.commit()
    return report


__all__ = ["load_legacy_translations", "import_title_translations", "TitleImportReport"]
