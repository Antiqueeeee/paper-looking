"""OpenAlex journal collector.

Fetches complete metadata for CCF journals of interest. Incremental mode uses
`from_publication_date`; first run (empty since) uses the publication-year
window, matching the original ACL-Anthology-Crawler behaviour.

Network access goes through `_api_get`, which is deliberately easy to monkeypatch
in tests. DOI is the canonical paper id.
"""
from __future__ import annotations

import logging
import time
import urllib.parse
from typing import Iterable

import requests

from paperbase.models import PaperDraft, SourceState

logger = logging.getLogger(__name__)

# (abbreviation, [ISSNs], display-name fallback, fixed OpenAlex source id)
JOURNALS: list[tuple[str, list[str], str, str | None]] = [
    ("TKDE", ["1041-4347", "1558-2191"], "IEEE Transactions on Knowledge and Data Engineering", None),
    ("TOIS", ["1046-8188", "1558-2868"], "ACM Transactions on Information Systems", None),
    ("VLDBJ", ["1066-8888", "0949-877X"], "The VLDB Journal", None),
    ("TODS", ["0362-5915", "1557-4644"], "ACM Transactions on Database Systems", None),
    ("TACL", ["2307-387X"], "Transactions of the Association for Computational Linguistics", None),
    ("JWS", ["1570-8268"], "Journal of Web Semantics", None),
    ("Computational Linguistics", ["0891-2017", "1530-9312"], "Computational Linguistics", None),
    ("KAIS", ["0219-1377", "0219-3116"], "Knowledge and Information Systems", None),
    ("TKDD", ["1556-4681"], "ACM Transactions on Knowledge Discovery from Data", None),
    ("DKE", ["0169-023X"], "Data and Knowledge Engineering", None),
    ("DMKD", ["1384-5810", "1573-756X"], "Data Mining and Knowledge Discovery", None),
    ("Information Sciences", ["0020-0255"], "Information Sciences", None),
    ("IS", ["0306-4379"], "Information Systems", None),
    ("IPM", ["0306-4573"], "Information Processing and Management", None),
    ("TWEB", ["1559-1131"], "ACM Transactions on the Web", None),
    ("DSE", ["2364-1185", "2364-1541"], "Data Science and Engineering", None),
    ("JAIR", ["1076-9757"], "Journal of Artificial Intelligence Research", None),
    ("Machine Learning", ["0885-6125", "1573-0565"], "Machine Learning", None),
    ("WWW", ["1386-145X", "1573-1413"], "World Wide Web", None),
    ("SCIS", ["1674-733X", "1869-1919"], "Science China Information Sciences", None),
    ("JASIST", ["2330-1635", "2330-1643"], "Journal of the Association for Information Science and Technology", None),
    ("KBS", ["0950-7051"], "Knowledge-Based Systems", None),
    ("IJSWIS", ["1552-6283", "1552-6291"], "International Journal on Semantic Web and Information Systems", None),
    ("NLE", ["2977-0424"], "Natural Language Processing", "S4404676627"),
    ("CSL", ["0885-2308"], "Computer Speech and Language", None),
    ("IJIS", ["0884-8173", "1098-111X"], "International Journal of Intelligent Systems", None),
    ("TIST", ["2157-6904"], "ACM Transactions on Intelligent Systems and Technology", None),
    ("JIIS", ["0925-9902", "1573-7675"], "Journal of Intelligent Information Systems", None),
    ("ESWA", ["0957-4174"], "Expert Systems with Applications", None),
    ("Applied Intelligence", ["0924-669X", "1573-7497"], "Applied Intelligence", None),
    ("Neurocomputing", ["0925-2312"], "Neurocomputing", None),
    ("Discover Computing", ["2948-2984", "2948-2992", "1386-4564"], "Discover Computing", None),
    ("TBD", ["2332-7790"], "IEEE Transactions on Big Data", None),
    ("TALLIP", ["2375-4699", "2375-4702"], "ACM Transactions on Asian and Low-Resource Language Information Processing", None),
    ("IJDAR", ["1433-2833", "1433-2825"], "International Journal on Document Analysis and Recognition", None),
]

API = "https://api.openalex.org"
PER_PAGE = 200
MAILTO = "research@example.com"


def api_get(url: str, *, timeout: int = 60, retries: int = 3) -> dict:
    """GET one OpenAlex URL, retrying transient failures."""
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            resp = requests.get(url, timeout=timeout, headers={"User-Agent": "paperbase/0.1"})
            if resp.status_code >= 400:
                raise RuntimeError(f"OpenAlex HTTP {resp.status_code}: {resp.text[:200]}")
            return resp.json()
        except (requests.RequestException, ValueError, RuntimeError) as exc:
            last_error = exc
            if attempt < retries - 1:
                time.sleep(2 + attempt)
    raise RuntimeError(f"OpenAlex GET failed: {last_error}") from last_error


def find_source(issn_list: list[str], name: str, source_id: str | None = None) -> dict | None:
    for issn in issn_list:
        data = api_get(f"{API}/sources?filter=issn:{issn}&per-page=1&mailto={MAILTO}")
        if data.get("results"):
            return data["results"][0]
        time.sleep(0.25)
    if source_id:
        data = api_get(f"{API}/sources/{source_id}?mailto={MAILTO}")
        if data.get("id"):
            return data
    data = api_get(f"{API}/sources?search={urllib.parse.quote(name)}&per-page=5&mailto={MAILTO}")
    for s in data.get("results", []):
        sn = s["display_name"].lower().replace("&", "and")
        tn = name.lower().replace("&", "and")
        if sn == tn or (sn in tn and "proceedings" not in sn and "conference" not in sn):
            return s
    return None


def rebuild_abstract(inverted: dict | None) -> str:
    if not inverted:
        return ""
    pos_map: dict[int, str] = {}
    for word, positions in inverted.items():
        for p in positions:
            pos_map[p] = word
    return " ".join(pos_map[i] for i in sorted(pos_map))


def work_to_draft(work: dict, abbr: str) -> PaperDraft | None:
    title = (work.get("title") or work.get("display_name") or "").strip()
    if not title:
        return None
    doi = (work.get("doi") or "").strip()
    for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
        if doi.startswith(prefix):
            doi = doi[len(prefix):]
    wid = (work.get("id") or "").strip()
    canonical = doi.lower() if doi.startswith("10.") else (f"openalex:{wid.split('/')[-1]}" if wid else "")
    if not canonical:
        return None

    authors = [
        (a.get("author") or {}).get("display_name", "")
        for a in (work.get("authorships") or [])
    ]
    authors = [a.strip() for a in authors if a.strip()]

    pdf_url = ""
    oa = work.get("best_oa_location") or work.get("primary_location") or {}
    candidate = (oa.get("pdf_url") or "").strip()
    if candidate.startswith("http"):
        pdf_url = candidate

    year = work.get("publication_year")
    return PaperDraft(
        id=canonical,
        source="openalex",
        title=title,
        authors=authors,
        abstract=rebuild_abstract(work.get("abstract_inverted_index")),
        year=int(year) if year else None,
        venue=abbr,
        url=f"https://doi.org/{doi}" if doi.startswith("10.") else work.get("id", ""),
        pdf_url=pdf_url,
        doi=doi if doi.startswith("10.") else "",
    )


class OpenAlexSource:
    name = "openalex"

    def __init__(self, years: Iterable[str] | None = None, journals: list | None = None):
        self.years = [str(y) for y in (years or ["2025", "2026"])]
        self.journals = journals or JOURNALS
        self.last_errors: list[str] = []

    def _filter_expr(self, source_id: str, since: str) -> str:
        year_filter = "publication_year:" + "|".join(self.years)
        if since:
            return f"primary_location.source.id:{source_id},from_publication_date:{since}"
        return f"primary_location.source.id:{source_id},{year_filter}"

    def _fetch_journal(self, abbr: str, issns: list[str], name: str, fixed_id: str | None, since: str) -> list[PaperDraft]:
        source = find_source(issns, name, fixed_id)
        if not source:
            raise RuntimeError(f"OpenAlex source not found for {abbr}")
        source_id = source["id"]
        filter_expr = self._filter_expr(source_id, since)
        select = (
            "id,doi,title,display_name,publication_year,authorships,"
            "abstract_inverted_index,best_oa_location,primary_location"
        )
        out: list[PaperDraft] = []
        cursor = "*"
        pages = 0
        while cursor:
            pages += 1
            if pages > 400:
                raise RuntimeError(f"{abbr}: exceeded 400 pages")
            url = (
                f"{API}/works?filter={urllib.parse.quote(filter_expr)}"
                f"&per-page={PER_PAGE}&cursor={urllib.parse.quote(cursor)}"
                f"&select={select}&mailto={MAILTO}"
            )
            data = api_get(url)
            for work in data.get("results", []):
                draft = work_to_draft(work, abbr)
                if draft:
                    out.append(draft)
            cursor = (data.get("meta") or {}).get("next_cursor")
            time.sleep(0.15)
        logger.info("openalex %s: %d papers in %d pages", abbr, len(out), pages)
        return out

    def fetch_incremental(self, since: str, state: SourceState) -> list[PaperDraft]:
        done = set(state.cursor.get("done_journals", []))
        out: list[PaperDraft] = []
        self.last_errors = []
        for abbr, issns, name, fixed_id in self.journals:
            if abbr in done:
                continue
            try:
                drafts = self._fetch_journal(abbr, issns, name, fixed_id, since)
                out.extend(drafts)
                done.add(abbr)
            except Exception as exc:
                self.last_errors.append(f"{abbr}: {exc}")
        state.cursor["done_journals"] = sorted(done)
        return out


__all__ = [
    "JOURNALS",
    "api_get",
    "find_source",
    "rebuild_abstract",
    "work_to_draft",
    "OpenAlexSource",
]
