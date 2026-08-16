"""Full-text Markdown translation with chunking, cache and budget control."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from paperbase.db import get_paper, set_local_file, set_translate_status, utcnow
from paperbase.paths import PaperPaths
from paperbase.pipeline.translate import (
    budget_limit,
    daily_budget_usage,
    make_llm_client,
)
from paperbase.tasks import fail_task, finish_task, release_task

FULL_PROMPT = """你是学术论文翻译器。将下面的英文 Markdown 翻译成简体中文。

要求：
1. 保持 Markdown 结构完全不变：标题层级、列表、表格、代码块、引用、LaTeX 公式、图片链接、引用标记都原样保留；
2. 只翻译正文文字，不翻译 URL、代码、数学公式和引用 key；
3. 术语第一次出现可保留英文原文，例如 GraphRAG、KBQA、entity alignment；
4. 不要添加原文没有的解释，不要输出任何前后缀说明，直接输出翻译后的 Markdown。

原文：
{text}"""


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def split_front_matter(text: str) -> tuple[str, str]:
    """Return (front_matter_with_delimiters, body)."""
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end != -1:
            close = end + 5
            return text[:close], text[close:]
    return "", text


def split_markdown_chunks(text: str, max_chars: int = 8000, min_chars: int = 4000) -> list[str]:
    """Split Markdown into chunks at blank-line/heading boundaries."""
    if len(text) <= max_chars:
        return [text] if text.strip() else []

    lines = text.splitlines(keepends=True)
    chunks: list[str] = []
    buf: list[str] = []
    size = 0
    for line in lines:
        if size >= min_chars and (line.strip() == "" or line.lstrip().startswith("#")):
            chunks.append("".join(buf))
            buf, size = [], 0
        buf.append(line)
        size += len(line)
        if size >= max_chars:
            chunks.append("".join(buf))
            buf, size = [], 0
    if buf:
        chunks.append("".join(buf))
    return chunks


def load_translation_cache(conn, paper_id: str) -> tuple[str, dict] | None:
    row = conn.execute(
        "SELECT input_hash, output FROM translation_cache WHERE paper_id=? AND kind='translate_full'",
        (paper_id,),
    ).fetchone()
    if not row:
        return None
    try:
        return row["input_hash"], json.loads(row["output"])
    except json.JSONDecodeError:
        return None


def save_translation_cache(conn, paper_id: str, source_hash: str, zh_path: str, model: str) -> None:
    conn.execute(
        """
        INSERT INTO translation_cache(paper_id, kind, input_hash, output, model, created_at)
        VALUES (?, 'translate_full', ?, ?, ?, ?)
        ON CONFLICT(paper_id, kind) DO UPDATE SET
            input_hash=excluded.input_hash,
            output=excluded.output,
            model=excluded.model,
            created_at=excluded.created_at
        """,
        (paper_id, source_hash, json.dumps({"zh_path": zh_path}, ensure_ascii=False), model, utcnow()),
    )
    conn.commit()


def translate_markdown(
    client,
    text: str,
    budget_tag: str = "translate_full",
    *,
    max_chars: int = 8000,
    min_chars: int = 4000,
) -> str:
    """Translate all chunks sequentially. Raises on any chunk failure."""
    chunks = split_markdown_chunks(text, max_chars=max_chars, min_chars=min_chars)
    out: list[str] = []
    for chunk in chunks:
        resp = client.chat(
            [{"role": "user", "content": FULL_PROMPT.format(text=chunk)}],
            temperature=0.1,
            budget_tag=budget_tag,
        )
        translated = (resp.content or "").strip()
        if not translated:
            raise ValueError("LLM returned empty translation for a chunk")
        out.append(translated)
    return "\n\n".join(out)


def run_translate_full_task(
    conn,
    config: dict,
    paths: PaperPaths,
    task: dict,
    *,
    client=None,
) -> None:
    """Execute one claimed translate_full task."""
    task_id = int(task["id"])
    paper_id = task["paper_id"]
    paper = get_paper(conn, paper_id)
    if paper is None:
        fail_task(conn, task_id, f"paper {paper_id} not found")
        return

    md_path = Path(paper.get("md_path") or (task.get("payload") or {}).get("md_path") or "")
    if not md_path.exists():
        fail_task(conn, task_id, f"source markdown not found: {md_path}")
        return

    source_text = md_path.read_text(encoding="utf-8")
    source_hash = sha256_text(source_text)
    zh_path = paths.paper_zh_md(paper)

    cached = load_translation_cache(conn, paper_id)
    if cached and cached[0] == source_hash and zh_path.exists():
        set_local_file(conn, paper_id, md_zh_path=str(zh_path))
        set_translate_status(conn, paper_id, "done")
        finish_task(conn, task_id, {"zh_path": str(zh_path), "cache": "hit"})
        return

    budget_tag = "translate_full"
    limit = budget_limit(config, budget_tag)
    used = daily_budget_usage(conn, budget_tag)
    estimated = max(1, len(source_text) // 3)
    if limit > 0 and used + estimated > limit:
        release_task(conn, task_id, "daily translation budget exhausted")
        return

    if client is None:
        client = make_llm_client(config, conn)
    try:
        set_translate_status(conn, paper_id, "running")
        front_matter, body = split_front_matter(source_text)
        if not body.strip():
            raise ValueError("source markdown has no translatable body")
        tr_cfg = config.get("translation", {})
        translated_body = translate_markdown(
            client,
            body,
            budget_tag=budget_tag,
            max_chars=int(tr_cfg.get("full_chunk_max_chars", 8000)),
            min_chars=int(tr_cfg.get("full_chunk_min_chars", 4000)),
        )

        tmp = zh_path.with_suffix(".zh.md.part")
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(front_matter + translated_body + "\n", encoding="utf-8")
        tmp.replace(zh_path)

        save_translation_cache(conn, paper_id, source_hash, str(zh_path), getattr(client, "model", ""))
        set_local_file(conn, paper_id, md_zh_path=str(zh_path))
        set_translate_status(conn, paper_id, "done")
        finish_task(conn, task_id, {"zh_path": str(zh_path), "chunks": 1})
    except Exception as exc:
        fail_task(conn, task_id, str(exc))
        row = conn.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row and row["status"] == "failed":
            set_translate_status(conn, paper_id, "failed")


__all__ = [
    "sha256_text",
    "split_front_matter",
    "split_markdown_chunks",
    "translate_markdown",
    "run_translate_full_task",
]
