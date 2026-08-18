"""Database persistence for per-profile interest decisions."""
from __future__ import annotations

import json

from paperbase.db import utcnow
from paperbase.interest.models import InterestDecision


class DatabaseInterestDecisionStore:
    """Persist decisions without coupling the classifier to one DB backend."""

    def __init__(self, conn):
        self.conn = conn

    def save(self, decision: InterestDecision) -> None:
        self.conn.execute(
            """
            INSERT INTO interest_decisions(
                paper_id, profile_id, label, score, matched_tags, reasons,
                method, model, classified_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(paper_id, profile_id) DO UPDATE SET
                label=excluded.label,
                score=excluded.score,
                matched_tags=excluded.matched_tags,
                reasons=excluded.reasons,
                method=excluded.method,
                model=excluded.model,
                classified_at=excluded.classified_at
            """,
            (
                decision.paper_id,
                decision.profile_id,
                decision.label,
                decision.score,
                json.dumps(list(decision.matched_tags), ensure_ascii=False),
                json.dumps(list(decision.reasons), ensure_ascii=False),
                decision.method,
                decision.model,
                utcnow(),
            ),
        )
        self.conn.commit()


def classify_database(conn, config: dict, *, profile_id: str | None = None, paper_ids=None, client=None):
    """Classify selected papers and persist one decision per profile."""
    from paperbase.interest import classify_papers, profile_from_config

    profile = profile_from_config(config, profile_id)
    query = "SELECT * FROM papers"
    params: tuple = ()
    if paper_ids is not None:
        ids = [str(value) for value in paper_ids if str(value).strip()]
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        query += f" WHERE id IN ({placeholders})"
        params = tuple(ids)
    papers = [dict(row) for row in conn.execute(query, params).fetchall()]
    decisions = classify_papers(
        papers,
        profile,
        client=client,
        store=DatabaseInterestDecisionStore(conn),
    )
    return decisions


__all__ = ["DatabaseInterestDecisionStore", "classify_database"]
