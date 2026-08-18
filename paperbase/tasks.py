"""Task queue stored in the configured relational database.

The queue is intentionally simple: a single worker process consumes it, but
claiming is transactional so multiple workers are safe too.

Idempotency contract: every task is unique on
`(paper_id, task_type, input_hash)`. Producers compute input_hash from the
task input (usually a file/content hash); calling enqueue_task twice with the
same key returns the existing task instead of creating a duplicate.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

from .db import dumps_json, utcnow
from .models import TaskType


def content_hash(*parts: object) -> str:
    """Stable hash for task deduplication."""
    h = hashlib.sha1()
    for part in parts:
        if isinstance(part, (dict, list)):
            part = json.dumps(part, ensure_ascii=False, sort_keys=True)
        h.update(str(part).encode("utf-8"))
    return h.hexdigest()


def enqueue_task(
    conn,
    *,
    paper_id: str,
    task_type: str | TaskType,
    payload: Mapping[str, Any] | None = None,
    input_hash: str | None = None,
    priority: int = 5,
    max_attempts: int = 3,
) -> int:
    """Create a task idempotently. Returns task id (new or existing)."""
    task_type = task_type.value if isinstance(task_type, TaskType) else str(task_type)
    payload = dict(payload or {})
    if input_hash is None:
        input_hash = content_hash(paper_id, task_type, payload)
    now = utcnow()

    cur = conn.execute(
        """
        INSERT INTO tasks(
            paper_id, task_type, status, payload, input_hash,
            priority, max_attempts, created_at
        ) VALUES (?, ?, 'queued', ?, ?, ?, ?, ?)
        ON CONFLICT(paper_id, task_type, input_hash) DO NOTHING
        RETURNING id
        """,
        (
            paper_id,
            task_type,
            dumps_json(payload),
            input_hash,
            int(priority),
            int(max_attempts),
            now,
        ),
    )
    created = cur.fetchone()
    conn.commit()
    if created:
        return int(created["id"])
    row = conn.execute(
        "SELECT id FROM tasks WHERE paper_id=? AND task_type=? AND input_hash=?",
        (paper_id, task_type, input_hash),
    ).fetchone()
    if row is None:
        raise RuntimeError("failed to create or load task")
    return int(row["id"])


def claim_next_task(
    conn,
    *,
    task_type: str | None = None,
    limit: int = 1,
) -> list[dict]:
    """Atomically claim the highest-priority queued task(s)."""
    if limit < 1:
        return []
    query = """
        SELECT id FROM tasks
         WHERE status='queued'
    """
    params: list[Any] = []
    if task_type:
        query += " AND task_type=?"
        params.append(task_type)
    query += " ORDER BY priority ASC, created_at ASC LIMIT ?"
    params.append(limit)
    if getattr(conn, "backend", "sqlite") == "postgresql":
        query += " FOR UPDATE SKIP LOCKED"

    claimed: list[dict] = []
    try:
        # psycopg opens a transaction for a preceding read. Close it before
        # explicitly opening the short task-claim transaction.
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        ids = [r["id"] for r in conn.execute(query, params).fetchall()]
        for task_id in ids:
            now = utcnow()
            cur = conn.execute(
                """
                UPDATE tasks
                   SET status='running', attempts=attempts+1, started_at=?,
                       last_error=''
                 WHERE id=? AND status='queued'
                """,
                (now, task_id),
            )
            if cur.rowcount == 1:
                claimed.append(task_to_dict(conn.execute(
                    "SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return claimed


def task_to_dict(row) -> dict:
    """Convert a task row into a plain dict with `payload` already parsed."""
    d = dict(row)
    try:
        d["payload"] = json.loads(d.get("payload") or "{}")
    except (json.JSONDecodeError, TypeError):
        d["payload"] = {}
    return d


def finish_task(conn: sqlite3.Connection, task_id: int, result: Mapping[str, Any] | None = None) -> None:
    now = utcnow()
    row = conn.execute("SELECT payload FROM tasks WHERE id=?", (task_id,)).fetchone()
    old_payload = {}
    if row:
        try:
            old_payload = json.loads(row["payload"] or "{}")
        except (json.JSONDecodeError, TypeError):
            old_payload = {}
    merged = dict(old_payload)
    if result:
        merged.update(dict(result))
    conn.execute(
        "UPDATE tasks SET status='done', finished_at=?, payload=? WHERE id=?",
        (now, dumps_json(merged), task_id),
    )
    conn.commit()


def fail_task(conn: sqlite3.Connection, task_id: int, error: str) -> None:
    """Mark failed. Requeues once if attempts remain, otherwise stays failed."""
    now = utcnow()
    row = conn.execute("SELECT attempts, max_attempts FROM tasks WHERE id=?", (task_id,)).fetchone()
    if row is None:
        return
    attempts = int(row["attempts"])
    max_attempts = int(row["max_attempts"])
    new_status = "queued" if attempts < max_attempts else "failed"
    conn.execute(
        """
        UPDATE tasks
           SET status=?, last_error=?, finished_at=?
         WHERE id=?
        """,
        (new_status, error[:2000], now if new_status == "failed" else None, task_id),
    )
    conn.commit()


def release_task(conn: sqlite3.Connection, task_id: int, error: str = "") -> None:
    """Return a claimed task to queued without consuming a retry attempt."""
    conn.execute(
        "UPDATE tasks SET status='queued', started_at=NULL, last_error=? WHERE id=?",
        (error[:2000], task_id),
    )
    conn.commit()


def cancel_task(conn: sqlite3.Connection, task_id: int) -> None:
    conn.execute("UPDATE tasks SET status='cancelled', finished_at=? WHERE id=?", (utcnow(), task_id))
    conn.commit()


def pending_count(conn: sqlite3.Connection, task_type: str | None = None) -> int:
    if task_type:
        row = conn.execute(
            "SELECT COUNT(*) AS total FROM tasks WHERE status='queued' AND task_type=?",
            (task_type,),
        ).fetchone()
    else:
        row = conn.execute("SELECT COUNT(*) AS total FROM tasks WHERE status='queued'").fetchone()
    return int(row["total"])


def reset_running_tasks(conn: sqlite3.Connection) -> int:
    """Return stale 'running' tasks to 'queued' after a process restart."""
    cur = conn.execute(
        "UPDATE tasks SET status='queued', started_at=NULL WHERE status='running'"
    )
    conn.commit()
    return cur.rowcount


__all__ = [
    "content_hash",
    "enqueue_task",
    "claim_next_task",
    "task_to_dict",
    "finish_task",
    "fail_task",
    "release_task",
    "cancel_task",
    "pending_count",
    "reset_running_tasks",
]
