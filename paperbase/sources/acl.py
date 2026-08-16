"""ACL Anthology yearly collector.

Reuses the proven parsing strategy of ACL-Anthology-Crawler/crawl_year.py but
writes normalized PaperDraft objects through the Wave 0 contract instead of
touching JSONL files directly.

Resumability: `state["done"]` holds already-fetched volume ids. The caller
persists state through `paperbase.db.get_meta/set_meta`; failed volumes are
simply not added to the done set, so the next run retries them.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable, Iterable

import requests
from bs4 import BeautifulSoup

from paperbase.models import PaperDraft, SourceState

logger = logging.getLogger(__name__)

VOLUMES_URL = "https://aclanthology.org/volumes/"
DEFAULT_TIMEOUT = 120
DEFAULT_RETRIES = 3


def get_bs_soup_from_url(url: str, *, timeout: int = DEFAULT_TIMEOUT, retries: int = DEFAULT_RETRIES) -> BeautifulSoup:
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            resp = requests.get(url, timeout=timeout, headers={"User-Agent": "paperbase/0.1"})
            resp.raise_for_status()
            resp.encoding = resp.apparent_encoding or resp.encoding
            return BeautifulSoup(resp.text, "html.parser")
        except (requests.RequestException, ValueError) as exc:
            last_error = exc
            if attempt < retries - 1:
                time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"GET {url} failed: {last_error}") from last_error


def _read_json(path: str | Path) -> list[dict]:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def get_volumes_for_years(years: Iterable[str], cache_path: str | Path | None = None) -> list[dict]:
    """Return volume dicts `{id, name}` for ALL requested years.

    The volume-list page is fetched only when the cache does not already
    cover every requested year.
    """
    years = {str(y) for y in years}
    cached: list[dict] = []
    if cache_path and Path(cache_path).exists():
        cached = _read_json(cache_path)
        cached_years = {str(v["id"])[:4] for v in cached if str(v["id"])[:4].isdigit()}
        if years.issubset(cached_years):
            return sorted((v for v in cached if str(v["id"])[:4] in years), key=lambda v: v["id"])

    soup = get_bs_soup_from_url(VOLUMES_URL)
    volumes: list[dict] = []
    for a in soup.find_all("a", href=re.compile(r"^/volumes/")):
        vid = a["href"].split("/")[2]
        m = re.match(r"^(19|20)\d\d", vid)
        if m and m.group(0) in years:
            volumes.append({"id": vid, "name": a.get_text().strip()})
    volumes.sort(key=lambda v: v["id"])

    # Keep cached entries we don't currently parse so done-skipping stays stable.
    seen = {v["id"] for v in volumes}
    volumes.extend(v for v in cached if v["id"] not in seen)
    volumes.sort(key=lambda v: v["id"])

    if cache_path:
        Path(cache_path).parent.mkdir(parents=True, exist_ok=True)
        Path(cache_path).write_text(
            json.dumps(volumes, ensure_ascii=False, indent=1), encoding="utf-8"
        )
    return [v for v in volumes if str(v["id"])[:4] in years]


def parse_volume_papers(soup: BeautifulSoup, volume_id: str) -> list[dict]:
    """Parse title/authors/abstract/pdf URL from a volume page."""
    rows = soup.find_all("div", class_="d-sm-flex align-items-stretch mb-3")
    cards = soup.find_all("div", class_="abstract-collapse")

    card_num_to_abstract: dict[str, str] = {}
    for card in cards:
        m = re.search(r"--(\d+)$", card.get("id", ""))
        if not m:
            continue
        body = card.find(class_="card-body")
        card_num_to_abstract[m.group(1)] = body.get_text().strip() if body else ""

    papers = []
    for row in rows:
        badge = row.find("a", class_="badge", href=re.compile(r"\.pdf$"))
        if badge is None:
            continue
        pdf_url = badge["href"]
        num_match = re.search(r"\.(\d+)\.pdf$", pdf_url)
        if num_match is None:
            continue
        num = num_match.group(1)
        if num == "0":
            continue  # front matter, not a paper

        span = row.find("span", class_="d-block")
        if span is None or span.strong is None or span.strong.a is None:
            continue
        strong_a = span.strong.a
        title = strong_a.get_text().strip()
        url = "https://aclanthology.org" + strong_a["href"]
        anthology_id = url.strip("/").split("/")[-1]
        authors = [
            a.get_text().strip()
            for a in span.find_all("a")
            if a.find_parent("strong") is None
        ]
        papers.append({
            "id": anthology_id,
            "title": title,
            "authors": authors,
            "url": url,
            "pdf_url": pdf_url,
            "abstract": card_num_to_abstract.get(num, ""),
            "year": int(str(anthology_id).split(".")[0][:4]),
            "volume": volume_id,
        })
    return papers


class ACLSource:
    name = "acl"

    def __init__(self, years: Iterable[str] | None = None, concurrency: int = 4, cache_path: str | Path | None = None):
        self.years = [str(y) for y in (years or [])]
        self.concurrency = max(1, int(concurrency))
        self.cache_path = Path(cache_path) if cache_path else None
        self.last_errors: list[str] = []

    def fetch_incremental(self, since: str, state: SourceState) -> list[PaperDraft]:
        """Fetch all not-yet-done volumes for configured years.

        Updates `state.cursor["done"]` in-place, so the caller's persistent
        checkpoint keeps resumability across process restarts.
        """
        drafts = self.fetch_papers(self.years, set(state.cursor.get("done", [])), progress=None)
        state.cursor["done"] = sorted(set(state.cursor.get("done", [])) | self._last_done)
        return drafts

    def fetch_papers(
        self,
        years: Iterable[str],
        done_volumes: set[str],
        progress: Callable[[str, int], None] | None = None,
    ) -> list[PaperDraft]:
        years = list(years) or self.years
        done = set(done_volumes)
        self._last_done: set[str] = set()
        volumes = get_volumes_for_years(years, cache_path=self.cache_path)
        todo = [v for v in volumes if v["id"] not in done]
        self.last_errors = []

        if not todo:
            return []

        out: list[PaperDraft] = []
        lock = threading.Lock()
        completed = 0

        def _fetch_one(volume: dict) -> tuple[str, list[PaperDraft] | None, str | None]:
            try:
                soup = get_bs_soup_from_url(f"https://aclanthology.org/volumes/{volume['id']}/")
                rows = parse_volume_papers(soup, volume["id"])
                drafts = [PaperDraft(**row, source="acl", venue=row.pop("volume"), tags=[]) for row in rows]
                return volume["id"], drafts, None
            except Exception as exc:  # per-volume failure should not abort the run
                return volume["id"], None, f"{volume['id']}: {exc}"

        with ThreadPoolExecutor(max_workers=self.concurrency) as pool:
            futures = {pool.submit(_fetch_one, v): v for v in todo}
            for fut in as_completed(futures):
                volume_id, drafts, error = fut.result()
                if error:
                    self.last_errors.append(error)
                    continue
                if drafts is not None:
                    with lock:
                        out.extend(drafts)
                        done.add(volume_id)
                        self._last_done.add(volume_id)
                        completed += 1
                        if progress:
                            progress(volume_id, completed)
        return out

__all__ = [
    "ACLSource",
    "get_bs_soup_from_url",
    "get_volumes_for_years",
    "parse_volume_papers",
]
