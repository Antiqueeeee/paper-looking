"""Collector registry and fetch orchestration.

`fetch_source()` is the only entry point the worker needs: it loads the
source-specific checkpoint, calls the collector, bulk-upserts drafts and
updates `fetch_runs`/`meta` state.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from paperbase.db import bulk_upsert_papers, count_papers, get_meta, set_meta, utcnow
from paperbase.models import FetchStatus, PaperDraft, PaperSource, SourceState
from paperbase.sources.acl import ACLSource
from paperbase.sources.arxiv import ArxivSource
from paperbase.sources.openalex import OpenAlexSource

@dataclass(frozen=True)
class CollectorPlugin:
    """Metadata and factory for one independently maintained collector."""

    name: str
    builder: object
    description: str = ""


_PLUGINS: dict[str, CollectorPlugin] = {}
# Kept as a compatibility view for existing integrations and tests.
_SOURCE_BUILDERS = {}
_EXTERNAL_LOADED = False


def register_source(name: str, builder, *, description: str = "") -> None:
    plugin = CollectorPlugin(name=name, builder=builder, description=description)
    _PLUGINS[name] = plugin
    _SOURCE_BUILDERS[name] = builder


def available_sources() -> list[str]:
    _ensure_external_plugins()
    return sorted(_PLUGINS)


def source_plugins() -> list[CollectorPlugin]:
    _ensure_external_plugins()
    return sorted(_PLUGINS.values(), key=lambda plugin: plugin.name)


def _ensure_external_plugins() -> None:
    global _EXTERNAL_LOADED
    if not _EXTERNAL_LOADED:
        _EXTERNAL_LOADED = True
        load_external_plugins()


def load_external_plugins() -> int:
    """Load optional third-party collectors from ``paperbase.sources`` entry points."""
    try:
        from importlib.metadata import entry_points

        entries = entry_points(group="paperbase.sources")
    except (ImportError, TypeError):
        entries = []
    loaded = 0
    for entry in entries:
        plugin = entry.load()
        if isinstance(plugin, CollectorPlugin):
            register_source(plugin.name, plugin.builder, description=plugin.description)
        elif callable(plugin):
            register_source(entry.name, plugin)
        else:
            raise TypeError(f"source entry point {entry.name!r} must provide a CollectorPlugin or builder")
        loaded += 1
    return loaded


def _build_acl(config: dict) -> ACLSource:
    return ACLSource(
        years=config.get("fetch", {}).get("years", []),
        concurrency=config.get("fetch", {}).get("concurrency", 4),
    )


def _build_openalex(config: dict) -> OpenAlexSource:
    return OpenAlexSource(years=config.get("fetch", {}).get("years", []))

register_source("acl", _build_acl, description="ACL Anthology conference proceedings")
register_source("openalex", _build_openalex, description="OpenAlex journal works")


def _build_arxiv(config: dict) -> ArxivSource:
    arxiv_cfg = config.get("arxiv", {}) or {}
    return ArxivSource(
        categories=arxiv_cfg.get("categories") or ["cs.CL", "cs.AI", "cs.IR"],
        keywords=arxiv_cfg.get("keywords") or [],
        max_results=int(arxiv_cfg.get("max_results", 200)),
    )

register_source("arxiv", _build_arxiv, description="arXiv category and keyword search")


@dataclass
class FetchReport:
    source: str
    status: str
    before: int = 0
    after: int = 0
    drafts: int = 0
    errors: list[str] = field(default_factory=list)
    message: str = ""

    @property
    def delta(self) -> int:
        return self.after - self.before


def get_source(name: str, config: dict) -> PaperSource:
    _ensure_external_plugins()
    builder = _SOURCE_BUILDERS.get(name)
    if builder is None:
        raise KeyError(f"unknown source: {name!r}; available={available_sources()}")
    return builder(config)


def load_source_state(conn, name: str) -> SourceState:
    raw = get_meta(conn, f"source_state:{name}", "{}")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        data = {}
    return SourceState(
        name=name,
        last_success_at=data.get("last_success_at"),
        cursor=data.get("cursor", {}),
        last_error=data.get("last_error", ""),
    )


def save_source_state(conn, state: SourceState, *, error: str = "") -> None:
    payload = {
        "last_success_at": state.last_success_at,
        "cursor": state.cursor,
        "last_error": error or state.last_error,
    }
    set_meta(conn, f"source_state:{state.name}", json.dumps(payload, ensure_ascii=False))


def fetch_source(conn, config: dict, name: str, *, since: str | None = None) -> FetchReport:
    """Run one source incrementally and persist its results."""
    source = get_source(name, config)
    if isinstance(source, ACLSource) and not getattr(source, "cache_path", None):
        data_dir = config.get("paths", {}).get("data_dir", "./data")
        source.cache_path = str(Path(data_dir) / "cache" / "acl" / "volumes.json")
    state = load_source_state(conn, name)
    before = count_papers(conn)
    now = utcnow()

    cur = conn.execute(
        "INSERT INTO fetch_runs(source, status, started_at) VALUES (?, ?, ?) RETURNING id",
        (name, FetchStatus.RUNNING.value, now),
    )
    run_id = int(cur.fetchone()["id"])
    conn.commit()

    report = FetchReport(source=name, status=FetchStatus.RUNNING.value, before=before)
    try:
        drafts = list(source.fetch_incremental(since or state.last_success_at or "", state))
        report.drafts = len(drafts)
        if drafts:
            rows = [d.to_paper_row() if isinstance(d, PaperDraft) else dict(d) for d in drafts]
            bulk_upsert_papers(conn, rows)
        state.last_success_at = now
        errors = list(getattr(source, "last_errors", []))
        if errors:
            report.status = FetchStatus.PARTIAL.value
            report.errors = errors
            report.message = f"{len(drafts)} drafts imported with {len(errors)} source errors"
        else:
            report.status = FetchStatus.SUCCESS.value
            report.message = f"{len(drafts)} drafts imported"
        save_source_state(conn, state, error="; ".join(errors[:5]))
    except Exception as exc:
        report.status = FetchStatus.FAILED.value
        report.message = str(exc)
        save_source_state(conn, state, error=str(exc))

    report.after = count_papers(conn)
    conn.execute(
        "UPDATE fetch_runs SET status=?, finished_at=?, new_count=?, fail_count=?, message=? WHERE id=?",
        (
            report.status,
            utcnow(),
            max(report.delta, 0),
            len(report.errors),
            report.message[:2000],
            run_id,
        ),
    )
    conn.commit()
    return report


__all__ = [
    "FetchReport",
    "CollectorPlugin",
    "register_source",
    "available_sources",
    "source_plugins",
    "load_external_plugins",
    "get_source",
    "fetch_source",
    "load_source_state",
    "save_source_state",
]
