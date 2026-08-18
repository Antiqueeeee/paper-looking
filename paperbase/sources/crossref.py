"""Crossref collector for the legacy NLE journal supplement.

The original crawler used Crossref because OpenAlex lagged for recent NLE
issues. This plugin keeps that source-specific behavior in the main pipeline.
It collects metadata and DOI/PDF links only; download tasks run separately.
"""
from __future__ import annotations

import re
import time
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime

import requests

from paperbase.models import PaperDraft, SourceState

API = "https://api.crossref.org/journals/{issn}/works"
XML_TAG_RE = re.compile(r"<[^>]+>")


def strip_tags(value: str) -> str:
    if not value:
        return ""
    value = re.sub(r"<jats:p>", " ", value)
    return re.sub(r"\s+", " ", XML_TAG_RE.sub("", value)).strip()


def work_to_draft(item: dict, venue: str = "NLE") -> PaperDraft | None:
    doi = str(item.get("DOI") or "").strip().lower()
    title = str((item.get("title") or [""])[0]).strip()
    if not doi or not title:
        return None
    authors = []
    for author in item.get("author") or []:
        name = " ".join(filter(None, [author.get("given", ""), author.get("family", "")])).strip()
        if name:
            authors.append(name)
    year = None
    for key in ("published", "published-print", "published-online", "issued"):
        parts = (item.get(key) or {}).get("date-parts") or []
        if parts and parts[0] and str(parts[0][0]).isdigit():
            year = int(parts[0][0])
            break
    url = f"https://doi.org/{doi}"
    pdf_url = ""
    for link in item.get("link") or []:
        candidate = str(link.get("URL") or "")
        if candidate.startswith("http") and "pdf" in str(link.get("content-type", "")).lower():
            pdf_url = candidate
            break
    return PaperDraft(
        id=doi,
        source="crossref",
        title=title,
        authors=authors,
        abstract=strip_tags(str(item.get("abstract") or "")),
        year=year,
        venue=venue,
        url=url,
        pdf_url=pdf_url,
        doi=doi,
    )


class CrossrefSource:
    name = "crossref"

    def __init__(
        self,
        *,
        issns: list[str] | None = None,
        years: list[int | str] | None = None,
        max_results: int = 2000,
        venue: str = "NLE",
    ):
        self.issns = [str(value) for value in (issns or ["1351-3249", "1469-8110"])]
        selected_years = [int(value) for value in (years or [2025, 2026])]
        self.start_date = f"{min(selected_years):04d}-01-01"
        self.end_date = f"{max(selected_years):04d}-12-31"
        self.max_results = max(1, int(max_results))
        self.venue = venue
        self.last_errors: list[str] = []

    def _fetch_issn(self, issn: str, since: str = "") -> list[PaperDraft]:
        start = since[:10] if since else self.start_date
        cursor = "*"
        out: list[PaperDraft] = []
        while cursor and len(out) < self.max_results:
            params = {
                "rows": min(1000, self.max_results - len(out)),
                "cursor": cursor,
                "filter": f"from-pub-date:{start},until-pub-date:{self.end_date},type:journal-article",
                "select": "DOI,title,author,abstract,published,published-print,published-online,issued,link",
            }
            url = f"{API.format(issn=urllib.parse.quote(issn))}?{urllib.parse.urlencode(params)}"
            response = requests.get(url, timeout=60, headers={"User-Agent": "paperbase/0.1 (mailto:research@example.com)"})
            response.raise_for_status()
            message = response.json().get("message", {})
            for item in message.get("items", []):
                draft = work_to_draft(item, self.venue)
                if draft:
                    out.append(draft)
            next_cursor = str(message.get("next-cursor") or "").strip()
            cursor = next_cursor if next_cursor and message.get("items") else ""
            if cursor:
                time.sleep(0.5)
        return out

    def fetch_incremental(self, since: str, state: SourceState) -> list[PaperDraft]:
        self.last_errors = []
        drafts: dict[str, PaperDraft] = {}
        for issn in self.issns:
            try:
                for draft in self._fetch_issn(issn, since):
                    drafts[draft.id] = draft
            except (requests.RequestException, ValueError, ET.ParseError) as exc:
                self.last_errors.append(f"{issn}: {exc}")
        state.cursor["last_success_at"] = datetime.utcnow().isoformat(timespec="seconds")
        return list(drafts.values())


__all__ = ["CrossrefSource", "strip_tags", "work_to_draft"]
