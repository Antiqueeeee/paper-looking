"""Optional arXiv collector (Atom API).

The source is keyword/category driven and best-effort. It is deliberately
kept small: arXiv metadata can always be re-fetched, so state only records
the last successful run timestamp.
"""
from __future__ import annotations

import logging
import time
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta

import requests

from paperbase.models import PaperDraft, SourceState

logger = logging.getLogger(__name__)

API = "https://export.arxiv.org/api/query"
ATOM = "{http://www.w3.org/2005/Atom}"


def parse_arxiv_atom(xml_text: str) -> list[PaperDraft]:
    root = ET.fromstring(xml_text)
    drafts: list[PaperDraft] = []
    for entry in root.findall(f"{ATOM}entry"):
        try:
            abs_url = entry.findtext(f"{ATOM}id", "").strip()
            arxiv_id = abs_url.split("/abs/")[-1].split("v")[0]
            title = " ".join(entry.findtext(f"{ATOM}title", "").split())
            summary = " ".join(entry.findtext(f"{ATOM}summary", "").split())
            authors = [
                " ".join(a.findtext(f"{ATOM}name", "").split())
                for a in entry.findall(f"{ATOM}author")
            ]
            published = entry.findtext(f"{ATOM}published", "").strip()
            year = int(published[:4]) if published[:4].isdigit() else None
            cat_el = entry.find(f"{ATOM}primary_category")
            primary = cat_el.get("term", "") if cat_el is not None else ""
            pdf_url = f"http://arxiv.org/pdf/{arxiv_id}.pdf"
            if not arxiv_id or not title:
                continue
            drafts.append(PaperDraft(
                id=f"arxiv:{arxiv_id}",
                source="arxiv",
                title=title,
                authors=[a for a in authors if a],
                abstract=summary,
                year=year,
                venue=primary or "arxiv",
                url=abs_url,
                pdf_url=pdf_url,
                extra={"published": published},
            ))
        except Exception as exc:  # one malformed entry must not kill the feed
            logger.debug("skip arxiv entry: %s", exc)
            continue
    return drafts


class ArxivSource:
    name = "arxiv"

    def __init__(
        self,
        *,
        categories: list[str] | None = None,
        keywords: list[str] | None = None,
        max_results: int = 200,
    ):
        self.categories = categories or ["cs.CL", "cs.AI", "cs.IR"]
        self.keywords = keywords or []
        self.max_results = max(1, int(max_results))
        self.last_errors: list[str] = []

    def fetch_incremental(self, since: str, state: SourceState) -> list[PaperDraft]:
        # Default window: last 7 days when no checkpoint exists.
        if not since and state.cursor.get("last_success_at"):
            since = str(state.cursor["last_success_at"])
        end = datetime.utcnow()
        start = datetime.fromisoformat(since) if since else end - timedelta(days=7)

        cat_clause = " OR ".join(f"cat:{c}" for c in self.categories)
        date_range = (
            f"submittedDate:[{start.strftime('%Y%m%d%H%M')} TO {end.strftime('%Y%m%d%H%M')}]"
        )
        query = f"({cat_clause}) AND {date_range}"
        if self.keywords:
            kw_clause = " OR ".join(f'all:"{k}"' for k in self.keywords)
            query = f"({cat_clause}) AND {date_range} AND ({kw_clause})"

        url = (
            f"{API}?search_query={urllib.parse.quote(query)}"
            f"&start=0&max_results={self.max_results}&sortBy=submittedDate&sortOrder=descending"
        )
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                resp = requests.get(url, timeout=60, headers={"User-Agent": "paperbase/0.1"})
                resp.raise_for_status()
                drafts = parse_arxiv_atom(resp.text)
                state.cursor["last_success_at"] = end.isoformat(timespec="seconds")
                return drafts
            except (requests.RequestException, ET.ParseError) as exc:
                last_error = exc
                if attempt < 2:
                    time.sleep(2 + attempt)
        raise RuntimeError(f"arXiv fetch failed: {last_error}") from last_error


__all__ = ["parse_arxiv_atom", "ArxivSource"]
