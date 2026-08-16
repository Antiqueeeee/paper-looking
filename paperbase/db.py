"""SQLite schema, connection factory and basic paper CRUD.

Contract:
  * business code never executes CREATE TABLE outside this module;
  * schema changes go through `migrate()` and bump SCHEMA_VERSION;
  * all JSON list/dict fields are stored as JSON text and decoded by helpers.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

SCHEMA_VERSION = 1

SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS papers (
    id               TEXT PRIMARY KEY,
    source           TEXT NOT NULL,
    title            TEXT NOT NULL,
    title_zh         TEXT NOT NULL DEFAULT '',
    authors          TEXT NOT NULL DEFAULT '[]',
    abstract         TEXT NOT NULL DEFAULT '',
    abstract_zh      TEXT NOT NULL DEFAULT '',
    year             INTEGER,
    venue            TEXT NOT NULL DEFAULT '',
    url              TEXT NOT NULL DEFAULT '',
    pdf_url          TEXT NOT NULL DEFAULT '',
    doi              TEXT NOT NULL DEFAULT '',
    tags             TEXT NOT NULL DEFAULT '[]',
    user_tags        TEXT NOT NULL DEFAULT '[]',
    status           TEXT NOT NULL DEFAULT 'new',
    pdf_status       TEXT NOT NULL DEFAULT 'none',
    parse_status      TEXT NOT NULL DEFAULT 'none',
    translate_status TEXT NOT NULL DEFAULT 'none',
    note             TEXT NOT NULL DEFAULT '',
    local_pdf        TEXT NOT NULL DEFAULT '',
    object_key       TEXT NOT NULL DEFAULT '',
    md_path          TEXT NOT NULL DEFAULT '',
    md_zh_path       TEXT NOT NULL DEFAULT '',
    extra            TEXT NOT NULL DEFAULT '{}',
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_papers_year ON papers(year);
CREATE INDEX IF NOT EXISTS idx_papers_source ON papers(source);
CREATE INDEX IF NOT EXISTS idx_papers_venue ON papers(venue);
CREATE INDEX IF NOT EXISTS idx_papers_status ON papers(status);
CREATE INDEX IF NOT EXISTS idx_papers_pdf_status ON papers(pdf_status);

CREATE TABLE IF NOT EXISTS tasks (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    paper_id     TEXT NOT NULL,
    task_type    TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'queued',
    payload      TEXT NOT NULL DEFAULT '{}',
    input_hash   TEXT NOT NULL DEFAULT '',
    priority     INTEGER NOT NULL DEFAULT 5,
    attempts     INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    last_error   TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL,
    started_at   TEXT,
    finished_at  TEXT,
    UNIQUE(paper_id, task_type, input_hash)
);

CREATE INDEX IF NOT EXISTS idx_tasks_status_priority
    ON tasks(status, priority, created_at);
CREATE INDEX IF NOT EXISTS idx_tasks_paper ON tasks(paper_id);

CREATE TABLE IF NOT EXISTS fetch_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT NOT NULL,
    status      TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    new_count   INTEGER NOT NULL DEFAULT 0,
    fail_count  INTEGER NOT NULL DEFAULT 0,
    message     TEXT NOT NULL DEFAULT '',
    cursor_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS qa_logs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    mode         TEXT NOT NULL DEFAULT 'library',
    question     TEXT NOT NULL,
    paper_ids    TEXT NOT NULL DEFAULT '[]',
    answer       TEXT NOT NULL DEFAULT '',
    citations    TEXT NOT NULL DEFAULT '[]',
    confidence   REAL,
    tool_calls   INTEGER NOT NULL DEFAULT 0,
    prompt_tokens  INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    model        TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cost_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    budget_tag   TEXT NOT NULL DEFAULT 'default',
    model        TEXT NOT NULL DEFAULT '',
    prompt_tokens     INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    estimated_cost_usd REAL NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open SQLite with WAL and sensible pragmas."""
    conn = sqlite3.connect(str(db_path), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def migrate(conn: sqlite3.Connection) -> int:
    """Create/upgrade schema to the latest version."""
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version < 1:
        conn.executescript(SCHEMA_V1)
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        version = SCHEMA_VERSION
    conn.commit()
    return version


def init_db(db_path: str | Path) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db_path)
    migrate(conn)
    return conn


# ---- JSON helpers -----------------------------------------------------------
def dumps_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def loads_list(value: str | None) -> list:
    if not value:
        return []
    try:
        out = json.loads(value)
        return out if isinstance(out, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def loads_dict(value: str | None) -> dict:
    if not value:
        return {}
    try:
        out = json.loads(value)
        return out if isinstance(out, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


# ---- paper CRUD --------------------------------------------------------------
def upsert_paper(conn: sqlite3.Connection, paper: Mapping[str, Any]) -> str:
    """Insert or update one paper; commits the transaction.

    Existing user-owned fields (note, user_tags, status, local_pdf, object_key,
    md paths) are preserved; source-owned fields are refreshed.
    """
    with conn:
        _upsert_paper(conn, paper)
    return str(paper["id"])


def bulk_upsert_papers(conn: sqlite3.Connection, papers) -> int:
    """Fast import path: one transaction for many paper mappings.

    Returns the number of rows processed. Same preservation rules as
    `upsert_paper`, and is safe to call again with the same data (idempotent).
    """
    count = 0
    with conn:
        for paper in papers:
            _upsert_paper(conn, paper)
            count += 1
    return count


def _upsert_paper(conn: sqlite3.Connection, paper: Mapping[str, Any]) -> None:
    """Insert/update one row. Caller controls the transaction."""
    now = utcnow()
    pid = str(paper["id"])
    title = str(paper.get("title", "") or "")
    if not title.strip():
        raise ValueError(f"paper {pid!r}: title must not be empty")
    if not pid.strip():
        raise ValueError("paper id must not be empty")

    conn.execute(
        """
        INSERT INTO papers(
            id, source, title, authors, abstract, year, venue, url,
            pdf_url, doi, tags, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            title=excluded.title,
            authors=excluded.authors,
            abstract=excluded.abstract,
            year=excluded.year,
            venue=excluded.venue,
            url=excluded.url,
            pdf_url=excluded.pdf_url,
            doi=excluded.doi,
            tags=excluded.tags,
            updated_at=excluded.updated_at
        """,
        (
            pid,
            str(paper.get("source", "manual")),
            title,
            dumps_json(paper.get("authors", [])),
            str(paper.get("abstract", "") or ""),
            paper.get("year"),
            str(paper.get("venue", "") or ""),
            str(paper.get("url", "") or ""),
            str(paper.get("pdf_url", "") or ""),
            str(paper.get("doi", "") or ""),
            dumps_json(paper.get("tags", [])),
            now,
            now,
        ),
    )


def get_paper(conn: sqlite3.Connection, paper_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM papers WHERE id=?", (paper_id,)).fetchone()
    return row_to_paper(row) if row else None


def row_to_paper(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["authors"] = loads_list(d.get("authors"))
    d["tags"] = loads_list(d.get("tags"))
    d["user_tags"] = loads_list(d.get("user_tags"))
    d["extra"] = loads_dict(d.get("extra"))
    return d


def update_paper_status(conn: sqlite3.Connection, paper_id: str, status: str) -> None:
    conn.execute(
        "UPDATE papers SET status=?, updated_at=? WHERE id=?",
        (status, utcnow(), paper_id),
    )
    conn.commit()


def set_pdf_status(conn: sqlite3.Connection, paper_id: str, pdf_status: str) -> None:
    conn.execute(
        "UPDATE papers SET pdf_status=?, updated_at=? WHERE id=?",
        (pdf_status, utcnow(), paper_id),
    )
    conn.commit()


def set_parse_status(conn: sqlite3.Connection, paper_id: str, parse_status: str) -> None:
    conn.execute(
        "UPDATE papers SET parse_status=?, updated_at=? WHERE id=?",
        (parse_status, utcnow(), paper_id),
    )
    conn.commit()


def set_translate_status(conn: sqlite3.Connection, paper_id: str, status: str) -> None:
    conn.execute(
        "UPDATE papers SET translate_status=?, updated_at=? WHERE id=?",
        (status, utcnow(), paper_id),
    )
    conn.commit()


def set_user_tags(conn: sqlite3.Connection, paper_id: str, tags: Iterable[str]) -> None:
    conn.execute(
        "UPDATE papers SET user_tags=?, updated_at=? WHERE id=?",
        (dumps_json(list(tags)), utcnow(), paper_id),
    )
    conn.commit()


def set_note(conn: sqlite3.Connection, paper_id: str, note: str) -> None:
    conn.execute(
        "UPDATE papers SET note=?, updated_at=? WHERE id=?",
        (note, utcnow(), paper_id),
    )
    conn.commit()


def set_local_file(
    conn: sqlite3.Connection,
    paper_id: str,
    *,
    local_pdf: str | None = None,
    md_path: str | None = None,
    md_zh_path: str | None = None,
    object_key: str | None = None,
) -> None:
    """Update local file fields. `None` means 'keep the existing value'."""
    fields = {
        "local_pdf": local_pdf,
        "md_path": md_path,
        "md_zh_path": md_zh_path,
        "object_key": object_key,
    }
    sets = [f"{col}=?" for col, val in fields.items() if val is not None]
    if not sets:
        return
    conn.execute(
        f"UPDATE papers SET {', '.join(sets)}, updated_at=? WHERE id=?",
        (*[v for v in fields.values() if v is not None], utcnow(), paper_id),
    )
    conn.commit()


def count_papers(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0]


__all__ = [
    "SCHEMA_VERSION",
    "connect",
    "migrate",
    "init_db",
    "utcnow",
    "dumps_json",
    "loads_list",
    "loads_dict",
    "upsert_paper",
    "bulk_upsert_papers",
    "get_paper",
    "row_to_paper",
    "update_paper_status",
    "set_pdf_status",
    "set_parse_status",
    "set_translate_status",
    "set_user_tags",
    "set_note",
    "set_local_file",
    "count_papers",
]
