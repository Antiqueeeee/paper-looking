"""Import legacy JSONL collections produced by ACL-Anthology-Crawler.

Supported inputs:
  * {year}.jsonl                         ACL Anthology full-year records
  * journals_2025_2026.jsonl             OpenAlex/Crossref journal records

Both are merged into the canonical `papers` table with the canonical id as
deduplication key. Import is repeatable and reports duplicates/failures.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from paperbase.db import bulk_upsert_papers
from paperbase.models import PaperDraft

logger = logging.getLogger(__name__)

ACL_YEAR_RE = re.compile(r"^\d{4}\.jsonl$")
JOURNAL_FILE_RE = re.compile(r"^journals_\d{4}_\d{4}\.jsonl$")


@dataclass
class ImportReport:
    files: int = 0
    rows: int = 0
    imported: int = 0
    duplicates: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "files": self.files,
            "rows": self.rows,
            "imported": self.imported,
            "duplicates": self.duplicates,
            "skipped": self.skipped,
            "errors": len(self.errors),
        }


def discover_legacy_files(legacy_dir: str | Path) -> list[Path]:
    root = Path(legacy_dir)
    if not root.exists():
        return []
    out = []
    for path in sorted(root.iterdir()):
        if path.is_file() and (ACL_YEAR_RE.match(path.name) or JOURNAL_FILE_RE.match(path.name)):
            out.append(path)
    return out


def is_acl_file(path: Path) -> bool:
    return bool(ACL_YEAR_RE.match(path.name))


def _safe_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def normalize_acl_row(row: dict) -> PaperDraft | None:
    pid = (row.get("id") or "").strip()
    title = (row.get("title") or "").strip()
    if not pid or not title:
        return None
    year = _safe_int(row.get("year")) or _safe_int(str(pid).split(".")[0])
    return PaperDraft(
        id=pid,
        source="acl",
        title=title,
        authors=[str(a).strip() for a in (row.get("authors") or []) if str(a).strip()],
        abstract=(row.get("abstract") or "").strip(),
        year=year,
        venue=str(row.get("volume") or row.get("venue") or ""),
        url=(row.get("url") or "").strip(),
        pdf_url=(row.get("pdf_url") or "").strip(),
        doi="",
        tags=[],
    )


def normalize_journal_row(row: dict) -> PaperDraft | None:
    pid = (row.get("id") or "").strip()
    title = (row.get("title") or "").strip()
    if not pid or not title:
        return None
    venue = str(row.get("volume") or row.get("venue") or "")
    source = "crossref" if venue.upper() == "NLE" else "openalex"
    return PaperDraft(
        id=pid.lower() if pid.startswith("10.") else pid,
        source=source,
        title=title,
        authors=[str(a).strip() for a in (row.get("authors") or []) if str(a).strip()],
        abstract=(row.get("abstract") or "").strip(),
        year=_safe_int(row.get("year")),
        venue=venue,
        url=(row.get("url") or "").strip(),
        pdf_url=(row.get("pdf_url") or "").strip(),
        doi=pid if pid.startswith("10.") else "",
        tags=[str(t) for t in (row.get("tags") or [])],
    )


def _completeness(d: PaperDraft) -> int:
    score = len(d.abstract) * 4
    if d.pdf_url:
        score += 8
    if d.url:
        score += 4
    if d.authors:
        score += min(len(d.authors), 20)
    if d.doi:
        score += 2
    return score


def dedupe_drafts(drafts: list[PaperDraft]) -> list[PaperDraft]:
    """Keep the most complete record for each canonical id."""
    best: dict[str, PaperDraft] = {}
    for d in drafts:
        if d.id not in best or _completeness(d) > _completeness(best[d.id]):
            best[d.id] = d
    return list(best.values())


def iter_drafts(paths: list[Path]) -> Iterator[tuple[Path, PaperDraft | None, str | None]]:
    """Yield (file, draft_or_none, error)."""
    for path in paths:
        try:
            with open(path, encoding="utf-8") as f:
                for line_no, line in enumerate(f, 1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError as exc:
                        yield path, None, f"{path.name}:{line_no}: bad json: {exc}"
                        continue
                    if not isinstance(row, dict):
                        yield path, None, f"{path.name}:{line_no}: row is not object"
                        continue
                    draft = normalize_acl_row(row) if is_acl_file(path) else normalize_journal_row(row)
                    if draft is None:
                        yield path, None, f"{path.name}:{line_no}: row missing id/title"
                        continue
                    yield path, draft, None
        except OSError as exc:
            yield path, None, f"{path.name}: cannot read: {exc}"


def import_legacy(conn, legacy_dir: str | Path, error_log: str | Path | None = None) -> ImportReport:
    """Import all legacy files in one transaction per file."""
    report = ImportReport()
    paths = discover_legacy_files(legacy_dir)
    report.files = len(paths)
    if not paths:
        logger.warning("no legacy files found under %s", legacy_dir)

    error_lines: list[str] = []
    for path in paths:
        drafts = []
        for _file, draft, error in iter_drafts([path]):
            if error:
                report.skipped += 1
                error_lines.append(error)
                continue
            if draft is None:
                # Already reported by iter_drafts with a precise line number.
                continue
            report.rows += 1
            drafts.append(draft)
        try:
            drafts = dedupe_drafts(drafts)
            bulk_upsert_papers(conn, [d.to_paper_row() for d in drafts])
            report.imported += len(drafts)
        except Exception as exc:  # keep one bad batch from killing the whole import
            report.skipped += len(drafts)
            error_lines.append(f"{path.name}: import failed: {exc}")

    report.errors = error_lines[:500]
    report.duplicates = max(report.rows - report.imported - report.skipped, 0)
    if error_log:
        Path(error_log).parent.mkdir(parents=True, exist_ok=True)
        Path(error_log).write_text("\n".join(error_lines) + "\n", encoding="utf-8")
    return report


__all__ = [
    "ImportReport",
    "discover_legacy_files",
    "normalize_acl_row",
    "normalize_journal_row",
    "dedupe_drafts",
    "iter_drafts",
    "import_legacy",
]
