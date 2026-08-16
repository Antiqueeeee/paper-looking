"""Metadata translation with hash-based caching and daily budget control."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable

from paperbase.db import utcnow


def make_llm_client(config: dict, conn=None):
    """Build the configured provider client (DeepSeek default)."""
    from paperbase.llm_providers import build_llm_client

    return build_llm_client(config, conn)


def make_cost_logger(conn):
    def log(tag: str, model: str, usage, cost_usd: float) -> None:
        try:
            conn.execute(
                "INSERT INTO cost_events(budget_tag, model, prompt_tokens, completion_tokens, estimated_cost_usd, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (tag, model, usage.prompt_tokens, usage.completion_tokens, cost_usd, utcnow()),
            )
            conn.commit()
        except Exception:
            # Cost logging must never break the primary translation flow.
            pass

    return log


def daily_budget_usage(conn, tag: str) -> int:
    day_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    row = conn.execute(
        "SELECT COALESCE(SUM(prompt_tokens + completion_tokens), 0) AS n "
        "FROM cost_events WHERE budget_tag=? AND created_at>=?",
        (tag, day_start),
    ).fetchone()
    return int(row["n"])


def budget_limit(config: dict, tag: str) -> int:
    budgets = config.get("budgets", {})
    if tag in ("translate_meta", "translate_full"):
        return int(budgets.get("translate_daily_tokens", 5_000_000))
    if tag == "qa":
        return int(budgets.get("qa_daily_tokens", 1_000_000))
    return 0


def _input_hash(title: str, abstract: str) -> str:
    return hashlib.sha1(f"{title}\n{abstract}".encode("utf-8")).hexdigest()


META_PROMPT = """你是论文元数据翻译器。把下面英文论文的标题和摘要翻译成简体中文。
要求：
1. 准确、学术化，保留 GraphRAG、KBQA、RAG、LLM、entity alignment 等专有名词；
2. 摘要翻译为连贯中文段落，不添加原文没有的内容；
3. 只输出一个 JSON 对象，不要任何解释。

输入标题：{title}
输入摘要：{abstract}

输出格式：
{{"title_zh": "...", "abstract_zh": "..."}}"""


def parse_json_response(content: str) -> dict | None:
    if not content:
        return None
    text = content.strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        return None
    try:
        data = json.loads(text[start:end + 1])
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        return None


@dataclass
class TranslationStats:
    translated: int = 0
    cached: int = 0
    skipped_no_text: int = 0
    budget_blocked: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.translated + self.cached


def translate_meta_for_papers(
    conn,
    client,
    config: dict,
    paper_ids: Iterable[str],
    *,
    budget_tag: str = "translate_meta",
) -> TranslationStats:
    """Translate title/abstract for explicit paper ids, using cache."""
    ids = list(paper_ids)
    stats = TranslationStats()
    if not ids:
        return stats

    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"SELECT id, title, abstract FROM papers WHERE id IN ({placeholders})",
        ids,
    ).fetchall()

    limit = budget_limit(config, budget_tag)
    used = daily_budget_usage(conn, budget_tag)

    for row in rows:
        title = (row["title"] or "").strip()
        abstract = (row["abstract"] or "").strip()
        if not title:
            stats.skipped_no_text += 1
            continue

        h = _input_hash(title, abstract)
        cached = conn.execute(
            "SELECT input_hash, output FROM translation_cache WHERE paper_id=? AND kind=?",
            (row["id"], budget_tag),
        ).fetchone()
        if cached and cached["input_hash"] == h:
            data = parse_json_response(cached["output"])
            if data and data.get("title_zh"):
                _apply_meta_translation(conn, row["id"], data)
                stats.cached += 1
                continue

        estimated_tokens = max(1, (len(title) + len(abstract)) // 3)
        if limit > 0 and used + estimated_tokens > limit:
            stats.budget_blocked += 1
            continue

        try:
            resp = client.chat(
                [
                    {"role": "user", "content": META_PROMPT.format(title=title, abstract=abstract)},
                ],
                temperature=0.1,
                budget_tag=budget_tag,
            )
            data = parse_json_response(resp.content)
            if not data or not data.get("title_zh"):
                raise ValueError("LLM did not return valid title_zh JSON")
            conn.execute(
                """
                INSERT INTO translation_cache(paper_id, kind, input_hash, output, model, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(paper_id, kind) DO UPDATE SET
                    input_hash=excluded.input_hash,
                    output=excluded.output,
                    model=excluded.model,
                    created_at=excluded.created_at
                """,
                (row["id"], budget_tag, h, json.dumps(data, ensure_ascii=False), getattr(client, "model", ""), utcnow()),
            )
            _apply_meta_translation(conn, row["id"], data)
            stats.translated += 1
            used += resp.usage.total_tokens or estimated_tokens
        except Exception as exc:
            stats.errors.append(f"{row['id']}: {exc}")

    conn.commit()
    return stats


def _apply_meta_translation(conn, paper_id: str, data: dict) -> None:
    conn.execute(
        "UPDATE papers SET title_zh=?, abstract_zh=?, updated_at=? WHERE id=?",
        (
            str(data.get("title_zh") or "").strip(),
            str(data.get("abstract_zh") or "").strip(),
            utcnow(),
            paper_id,
        ),
    )


__all__ = [
    "make_llm_client",
    "make_cost_logger",
    "daily_budget_usage",
    "budget_limit",
    "parse_json_response",
    "translate_meta_for_papers",
    "TranslationStats",
    "META_PROMPT",
]
