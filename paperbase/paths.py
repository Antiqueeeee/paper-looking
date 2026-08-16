"""Single source of truth for every on-disk path.

Contract rules (see docs/IMPLEMENTATION_PLAN.md):

    DATA_DIR/
      papers.db
      md/{source}/{year}/{venue}/{paper_id}.md
      md/{source}/{year}/{venue}/{paper_id}.zh.md
      pdf/hot/{paper_id}.pdf
      pdf/uploads/{paper_id}.pdf
      tmp/{task_type}/{task_id}/
      cache/{owner}/

Business code must never assemble these paths by hand.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Mapping

_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")
_MAX_COMPONENT = 120


def safe_component(value: object, default: str = "unknown") -> str:
    """Make one path component safe and deterministic."""
    text = str(value or "").strip()
    if not text:
        text = default
    text = re.sub(r"[\\/:*?\"<>|]+", "_", text)
    text = _SAFE_RE.sub("_", text)
    text = text.strip("._")
    if not text:
        text = default
    return text[:_MAX_COMPONENT] or default


class PaperPaths:
    """All filesystem paths for the paper library."""

    def __init__(self, data_dir: str | Path):
        self.root = Path(data_dir).expanduser().resolve()

    # ---- top level directories -------------------------------------------------
    @property
    def db_path(self) -> Path:
        return self.root / "papers.db"

    @property
    def md_dir(self) -> Path:
        return self.root / "md"

    @property
    def pdf_hot_dir(self) -> Path:
        return self.root / "pdf" / "hot"

    @property
    def pdf_upload_dir(self) -> Path:
        return self.root / "pdf" / "uploads"

    @property
    def tmp_dir(self) -> Path:
        return self.root / "tmp"

    @property
    def cache_dir(self) -> Path:
        return self.root / "cache"

    # ---- per-paper paths --------------------------------------------------------
    def paper_md_rel(self, paper: Mapping | object) -> str:
        """Relative path under md_dir, e.g. acl/2026/findings-acl/id.md."""
        if isinstance(paper, Mapping):
            source = paper.get("source", "unknown")
            year = paper.get("year", "unknown")
            venue = paper.get("venue", "unknown")
            paper_id = paper.get("id", "unknown")
        else:
            source = getattr(paper, "source", "unknown")
            year = getattr(paper, "year", "unknown")
            venue = getattr(paper, "venue", "unknown")
            paper_id = getattr(paper, "id", "unknown")
        return (
            f"{safe_component(source)}/{safe_component(year)}/"
            f"{safe_component(venue)}/{safe_component(paper_id)}.md"
        )

    def paper_md(self, paper: Mapping | object) -> Path:
        return self.md_dir / self.paper_md_rel(paper)

    def paper_zh_rel(self, paper: Mapping | object) -> str:
        return self.paper_md_rel(paper).removesuffix(".md") + ".zh.md"

    def paper_zh_md(self, paper: Mapping | object) -> Path:
        return self.md_dir / self.paper_zh_rel(paper)

    def hot_pdf(self, paper_id: object) -> Path:
        return self.pdf_hot_dir / f"{safe_component(paper_id)}.pdf"

    def upload_pdf(self, paper_id: object) -> Path:
        return self.pdf_upload_dir / f"{safe_component(paper_id)}.pdf"

    def task_tmp(self, task_type: object, task_id: object) -> Path:
        return self.tmp_dir / safe_component(task_type) / str(task_id)

    def source_cache(self, owner: str) -> Path:
        return self.cache_dir / safe_component(owner)

    def corpus_dir(self) -> Path:
        """Root directory exposed read-only to the DCI agent."""
        return self.md_dir

    # ---- helpers -----------------------------------------------------------------
    def ensure_dirs(self) -> None:
        for p in (
            self.root,
            self.md_dir,
            self.pdf_hot_dir,
            self.pdf_upload_dir,
            self.tmp_dir,
            self.cache_dir,
        ):
            p.mkdir(parents=True, exist_ok=True)


__all__ = ["PaperPaths", "safe_component"]
