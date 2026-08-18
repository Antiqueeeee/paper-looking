"""Database schema, connection factory and basic paper CRUD.

Contract:
  * business code never executes CREATE TABLE outside this module;
  * schema changes go through `migrate()` and bump SCHEMA_VERSION;
  * all JSON list/dict fields are stored as JSON text and decoded by helpers.
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

SCHEMA_VERSION = 5

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

SCHEMA_V2 = """
CREATE TABLE IF NOT EXISTS translation_cache (
    paper_id   TEXT NOT NULL,
    kind       TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    output     TEXT NOT NULL,
    model      TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    PRIMARY KEY(paper_id, kind)
);
"""

SCHEMA_V3 = """
ALTER TABLE papers ADD COLUMN pdf_sha256 TEXT NOT NULL DEFAULT '';
"""

SCHEMA_V4 = """
ALTER TABLE qa_logs ADD COLUMN visibility TEXT NOT NULL DEFAULT 'public';
ALTER TABLE qa_logs ADD COLUMN account_id INTEGER;
"""

SCHEMA_V5 = """
CREATE TABLE IF NOT EXISTS interest_decisions (
    paper_id       TEXT NOT NULL,
    profile_id     TEXT NOT NULL,
    label          TEXT NOT NULL,
    score          REAL NOT NULL DEFAULT 0,
    matched_tags   TEXT NOT NULL DEFAULT '[]',
    reasons        TEXT NOT NULL DEFAULT '[]',
    method         TEXT NOT NULL DEFAULT 'rules',
    model          TEXT NOT NULL DEFAULT '',
    classified_at  TEXT NOT NULL,
    PRIMARY KEY (paper_id, profile_id)
);
CREATE INDEX IF NOT EXISTS idx_interest_profile_label
    ON interest_decisions(profile_id, label);
"""


class DatabaseConnection:
    """Small DB-API compatibility layer for SQLite tests and PostgreSQL.

    The application intentionally keeps its SQL close to the domain code. This
    adapter accepts the existing qmark parameters and presents mapping rows for
    both backends, while production PostgreSQL uses psycopg directly.
    """

    def __init__(self, raw, backend: str):
        self.raw = raw
        self.backend = backend

    def execute(self, sql: str, params: Any = None):
        if self.backend == "postgresql":
            sql = _postgres_sql(sql)
        return self.raw.execute(sql, () if params is None else params)

    def executescript(self, script: str):
        if self.backend == "sqlite":
            return self.raw.executescript(script)
        return self.raw.execute(_postgres_schema(script))

    def commit(self):
        return self.raw.commit()

    def rollback(self):
        return self.raw.rollback()

    def close(self):
        return self.raw.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.commit()
        else:
            self.rollback()
        return False

    def __getattr__(self, name: str):
        return getattr(self.raw, name)


def _qmark_to_pyformat(sql: str) -> str:
    """Convert qmark parameters without touching quoted string literals."""
    out: list[str] = []
    quote = ""
    i = 0
    while i < len(sql):
        char = sql[i]
        if quote:
            out.append(char)
            if char == quote:
                if i + 1 < len(sql) and sql[i + 1] == quote:
                    out.append(sql[i + 1])
                    i += 1
                else:
                    quote = ""
        elif char in ("'", '"'):
            quote = char
            out.append(char)
        elif char == "?":
            out.append("%s")
        else:
            out.append(char)
        i += 1
    return "".join(out)


def _postgres_sql(sql: str) -> str:
    sql = re.sub(r"\bBEGIN\s+IMMEDIATE\b", "BEGIN", sql, flags=re.IGNORECASE)
    sql = re.sub(r"\bINSERT\s+OR\s+IGNORE\s+INTO\b", "INSERT INTO", sql, flags=re.IGNORECASE)
    if re.search(r"\bINSERT\s+INTO\s+tasks\b", sql, re.IGNORECASE) and "ON CONFLICT" not in sql.upper():
        sql = re.sub(r"(VALUES\s*\([^;]+\))", r"\1 ON CONFLICT DO NOTHING", sql, count=1, flags=re.IGNORECASE | re.DOTALL)
    return _qmark_to_pyformat(sql)


def _postgres_schema(script: str) -> str:
    script = script.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "BIGSERIAL PRIMARY KEY")
    return script


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def is_postgres_target(target: str | Path) -> bool:
    return str(target).startswith(("postgres://", "postgresql://"))


def connect(target: str | Path) -> DatabaseConnection:
    """Open SQLite for local tests or PostgreSQL for application deployments."""
    if is_postgres_target(target):
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:
            raise RuntimeError("PostgreSQL support requires psycopg; install paperbase with its runtime dependencies") from exc
        raw = psycopg.connect(str(target), row_factory=dict_row)
        return DatabaseConnection(raw, "postgresql")

    path = Path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = sqlite3.connect(str(path), timeout=10, check_same_thread=False)
    raw.row_factory = sqlite3.Row
    raw.execute("PRAGMA journal_mode=WAL")
    raw.execute("PRAGMA busy_timeout=5000")
    raw.execute("PRAGMA synchronous=NORMAL")
    raw.execute("PRAGMA foreign_keys=ON")
    return DatabaseConnection(raw, "sqlite")


def migrate(conn: DatabaseConnection) -> int:
    """Create/upgrade schema to the latest version."""
    if conn.backend == "postgresql":
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        row = conn.execute("SELECT COALESCE(MAX(version), 0) AS version FROM schema_migrations").fetchone()
        version = int(row["version"])
        migrations = [(1, SCHEMA_V1), (2, SCHEMA_V2), (3, SCHEMA_V3), (4, SCHEMA_V4), (5, SCHEMA_V5)]
        for migration_version, schema in migrations:
            if version >= migration_version:
                continue
            if migration_version >= 3:
                schema = schema.replace(" ADD COLUMN ", " ADD COLUMN IF NOT EXISTS ")
            conn.executescript(schema)
            conn.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?) ON CONFLICT(version) DO NOTHING",
                (migration_version, utcnow()),
            )
            version = migration_version
        conn.commit()
        return version

    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version < 1:
        conn.executescript(SCHEMA_V1)
        version = 1
    if version < 2:
        conn.executescript(SCHEMA_V2)
        version = 2
    if version < 3:
        try:
            conn.executescript(SCHEMA_V3)
        except Exception:
            pass
        version = 3
    if version < 4:
        conn.executescript(SCHEMA_V4)
        version = 4
    if version < 5:
        conn.executescript(SCHEMA_V5)
        version = 5
    if version:
        conn.execute(f"PRAGMA user_version={version}")
    conn.commit()
    return version


def init_db(target: str | Path) -> DatabaseConnection:
    conn = connect(target)
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

    source = str(paper.get("source", "manual"))
    if source not in {"acl", "openalex", "crossref", "arxiv", "manual"}:
        raise ValueError(f"paper {pid!r}: invalid source {source!r}")

    year = paper.get("year")
    if year is not None:
        try:
            year = int(year)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"paper {pid!r}: invalid year {year!r}") from exc

    for field in ("url", "pdf_url"):
        value = str(paper.get(field, "") or "")
        if value and not value.startswith(("http://", "https://")):
            raise ValueError(f"paper {pid!r}: invalid {field} {value!r}")

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
            source,
            title,
            dumps_json(paper.get("authors", [])),
            str(paper.get("abstract", "") or ""),
            year,
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
    pdf_sha256: str | None = None,
) -> None:
    """Update local file fields. `None` means 'keep the existing value'."""
    fields = {
        "local_pdf": local_pdf,
        "md_path": md_path,
        "md_zh_path": md_zh_path,
        "object_key": object_key,
        "pdf_sha256": pdf_sha256,
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
    row = conn.execute("SELECT COUNT(*) AS total FROM papers").fetchone()
    return int(row["total"])


def get_meta(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
    conn.commit()


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
    "get_meta",
    "set_meta",
]
