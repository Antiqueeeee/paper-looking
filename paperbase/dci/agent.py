"""DCI question-answering agent (tool-calling loop)."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from paperbase.config import database_target
from paperbase.db import get_paper, utcnow
from paperbase.dci.prefilter import prefilter_parsed_papers
from paperbase.dci.prompts import (
    FINALIZE_PROMPT,
    system_prompt_compare,
    system_prompt_library,
    system_prompt_paper,
)
from paperbase.dci.tools import TOOL_SPECS, ToolContext, execute_tool
from paperbase.paths import PaperPaths
from paperbase.pipeline.translate import make_llm_client

CONF_RE = re.compile(r"Confidence\s*[:：]\s*(\d{1,3})")
CITE_RE = re.compile(r"\[([^\]\n:]+):([0-9][0-9,\s\-–]*)\]")


def parse_citations(text: str) -> list[str]:
    """Extract normalized citations, supporting [path:3], [path:3, 7] and ranges."""
    out: list[str] = []
    for m in CITE_RE.finditer(text):
        path = m.group(1).strip()
        nums = m.group(2).replace("–", "-")
        for token in re.split(r"[,，]", nums):
            token = token.strip()
            if not token:
                continue
            range_match = re.fullmatch(r"(\d+)\s*-\s*(\d+)", token)
            if range_match:
                citation = f"[{path}:{range_match.group(1)}-{range_match.group(2)}]"
            elif re.fullmatch(r"\d+", token):
                citation = f"[{path}:{token}]"
            else:
                continue
            if citation not in out:
                out.append(citation)
    return out


@dataclass
class QAAnswer:
    mode: str
    question: str
    answer: str = ""
    citations: list[str] = field(default_factory=list)
    confidence: float | None = None
    tool_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    paper_ids: list[str] = field(default_factory=list)
    error: str = ""


def _tool_definitions() -> list[dict]:
    return [{"type": "function", "function": spec} for spec in TOOL_SPECS]


def _resolve_md_path(paths: PaperPaths, value: str) -> Path | None:
    if not value:
        return None
    p = Path(value)
    if not p.is_absolute():
        p = paths.root / p
    return p if p.exists() else None


def _candidate_scope(paths: PaperPaths, papers: list[dict]) -> list[Path]:
    files: list[Path] = []
    for p in papers:
        for key in ("md_path", "md_zh_path"):
            f = _resolve_md_path(paths, p.get(key) or "")
            if f and f not in files:
                files.append(f)
    return files


class DCIQAAgent:
    def __init__(self, conn, config: dict, paths: PaperPaths, client=None):
        self.conn = conn
        self.config = config
        self.paths = paths
        self.client = client

    def _ensure_client(self):
        if self.client is None:
            self.client = make_llm_client(self.config, self.conn)
        return self.client

    def ask(
        self,
        question: str,
        *,
        mode: str = "library",
        paper_ids: list[str] | None = None,
        history: list[dict] | None = None,
    ) -> QAAnswer:
        paper_ids = [str(x) for x in (paper_ids or [])]
        max_tool_calls = int(self.config.get("dci", {}).get("max_tool_calls", 30))
        tool_output_chars = int(self.config.get("dci", {}).get("tool_output_chars", 12000))

        papers, system, error = self._prepare(mode, paper_ids, question)
        if error:
            ans = QAAnswer(mode=mode, question=question, answer=error, confidence=0.0, paper_ids=paper_ids, error=error)
            self._log(ans)
            return ans

        client = self._ensure_client()

        scope = _candidate_scope(self.paths, papers)
        if papers and not scope:
            ans = QAAnswer(mode=mode, question=question, answer="候选论文的 Markdown 文件缺失。", confidence=0.0, paper_ids=paper_ids)
            self._log(ans)
            return ans
        ctx = ToolContext(
            corpus_dir=self.paths.corpus_dir(),
            db_path=self.paths.db_path,
            database_target=database_target(self.config, self.paths.db_path),
            scope_files=scope,
            tool_output_chars=tool_output_chars,
        )

        user_content = f"问题：{question}"
        if history:
            turns = []
            for item in history[-6:]:
                q = str(item.get("question", "")).strip()
                a = str(item.get("answer", "")).strip()
                if q and a:
                    turns.append(f"Q: {q}\nA: {a[:3000]}")
            if turns:
                user_content = (
                    "以下是本次会话的最近问答，仅用于理解当前问题中的指代和上下文；"
                    "新回答仍然必须基于论文原文，不得把历史回答当作新证据。\n\n"
                    + "\n\n".join(turns)
                    + f"\n\n当前问题：{question}"
                )

        messages: list[dict] = [
            {"role": "system", "content": system},
            {"role": "user", "content": user_content},
        ]
        tools = _tool_definitions()
        answer_text = ""
        usage = {"prompt": 0, "completion": 0}
        steps = 0

        for step in range(max_tool_calls):
            resp = client.chat(messages, tools=tools, tool_choice="auto", budget_tag="qa")
            usage["prompt"] += resp.usage.prompt_tokens
            usage["completion"] += resp.usage.completion_tokens
            if not resp.tool_calls:
                answer_text = resp.content or ""
                break

            assistant_msg = {
                "role": "assistant",
                "content": resp.content or "",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.name, "arguments": tc.arguments},
                    }
                    for tc in resp.tool_calls
                ],
            }
            messages.append(assistant_msg)
            for tc in resp.tool_calls:
                try:
                    result = execute_tool(tc.name, _parse_args(tc.arguments), ctx)
                except Exception as exc:
                    result = f"error: {exc}"
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": ctx.truncate(result),
                })
                steps += 1
        else:
            # Tool budget exhausted: force a final answer from gathered evidence.
            messages.append({"role": "user", "content": FINALIZE_PROMPT})
            final = client.chat(messages, budget_tag="qa")
            usage["prompt"] += final.usage.prompt_tokens
            usage["completion"] += final.usage.completion_tokens
            answer_text = final.content or ""

        ans = self._build_answer(mode, question, answer_text, paper_ids, usage, steps)
        self._log(ans)
        return ans

    def _prepare(self, mode: str, paper_ids: list[str], question: str) -> tuple[list[dict], str, str]:
        if mode == "paper":
            if len(paper_ids) != 1:
                return [], "", "单篇问答需要且只能指定一篇论文。"
            paper = get_paper(self.conn, paper_ids[0])
            if paper is None:
                return [], "", f"论文不存在：{paper_ids[0]}"
            md = _resolve_md_path(self.paths, paper.get("md_path") or "")
            if md is None:
                return [], "", f"论文尚未解析，无法问答：{paper['title']}（{paper['id']}）"
            system = system_prompt_paper(self.paths.corpus_dir(), md, paper["title"])
            return [paper], system, ""

        if mode == "compare":
            papers = []
            for pid in paper_ids:
                p = get_paper(self.conn, pid)
                if p is None:
                    return [], "", f"论文不存在：{pid}"
                md = _resolve_md_path(self.paths, p.get("md_path") or "")
                if md is None:
                    return [], "", f"论文尚未解析，无法对比：{p['title']}（{pid}）"
                papers.append(p)
            system = system_prompt_compare(
                self.paths.corpus_dir(),
                [(_resolve_md_path(self.paths, p["md_path"]), p["title"]) for p in papers],
            )
            return papers, system, ""

        papers = prefilter_parsed_papers(self.conn, question, limit=200)
        if not papers:
            return [], "", "当前论文库中没有已解析且符合问题约束的候选论文。"
        system = system_prompt_library(
            self.paths.corpus_dir(), database_target(self.config, self.paths.db_path), _candidate_scope(self.paths, papers), mode="library"
        )
        return papers, system, ""

    def _build_answer(self, mode, question, text, paper_ids, usage, steps):
        citations = parse_citations(text)
        conf_match = CONF_RE.search(text)
        confidence = float(conf_match.group(1)) / 100.0 if conf_match else None
        return QAAnswer(
            mode=mode,
            question=question,
            answer=text.strip(),
            citations=list(dict.fromkeys(citations)),
            confidence=confidence,
            tool_calls=steps,
            prompt_tokens=usage["prompt"],
            completion_tokens=usage["completion"],
            paper_ids=list(paper_ids),
        )

    def _log(self, ans: QAAnswer) -> None:
        model = getattr(self.client, "model", "")
        self.conn.execute(
            """
            INSERT INTO qa_logs(
                mode, question, paper_ids, answer, citations, confidence,
                tool_calls, prompt_tokens, completion_tokens, model, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ans.mode,
                ans.question[:4000],
                json.dumps(ans.paper_ids, ensure_ascii=False),
                ans.answer[:20000],
                json.dumps(ans.citations, ensure_ascii=False),
                ans.confidence,
                ans.tool_calls,
                ans.prompt_tokens,
                ans.completion_tokens,
                model,
                utcnow(),
            ),
        )
        self.conn.commit()


def _parse_args(raw: str) -> dict:
    import json

    raw = (raw or "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def ask_paper(conn, config, paths, paper_id: str, question: str, client=None) -> QAAnswer:
    return DCIQAAgent(conn, config, paths, client).ask(question, mode="paper", paper_ids=[paper_id])


def ask_library(conn, config, paths, question: str, client=None) -> QAAnswer:
    return DCIQAAgent(conn, config, paths, client).ask(question, mode="library")


__all__ = ["QAAnswer", "DCIQAAgent", "ask_paper", "ask_library"]
