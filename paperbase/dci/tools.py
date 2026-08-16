"""Read-only corpus tools for the DCI agent.

Safety contract:
  * no shell string is ever constructed;
  * file tools are confined to the corpus root (and an optional scope);
  * sqlite_query opens a fresh read-only connection with `mode=ro`;
  * each tool output is truncated by the caller.
"""
from __future__ import annotations

import json
import re
import sqlite3
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

MAX_RG_RESULTS = 60
MAX_READ_LINES = 400
MAX_SQL_ROWS = 100

SELECT_RE = re.compile(r"^\s*(SELECT|WITH)\b", re.IGNORECASE)


class ToolSecurityError(RuntimeError):
    pass


@dataclass
class ToolContext:
    corpus_dir: Path
    db_path: Path
    scope_files: list[Path] = field(default_factory=list)
    tool_output_chars: int = 12000

    def _resolve_in_corpus(self, raw: str | Path) -> Path:
        p = Path(raw)
        if not p.is_absolute():
            p = self.corpus_dir / p
        p = p.resolve()
        try:
            p.relative_to(self.corpus_dir.resolve())
        except ValueError as exc:
            raise ToolSecurityError(f"path outside corpus: {raw}") from exc
        return p

    def _resolve_in_scope(self, raw: str | Path) -> Path:
        p = self._resolve_in_corpus(raw)
        if self.scope_files:
            roots = [r.resolve() for r in self.scope_files]
            allowed = any(p == r or p.is_relative_to(r) for r in roots)
            if not allowed:
                raise ToolSecurityError(f"path outside current question scope: {raw}")
        return p

    def truncate(self, text: str) -> str:
        if len(text) <= self.tool_output_chars:
            return text
        return text[: self.tool_output_chars] + f"\n...[truncated {len(text) - self.tool_output_chars} chars]"


def _short_path(ctx: ToolContext, p: Path) -> str:
    try:
        return str(p.resolve().relative_to(ctx.corpus_dir.resolve()))
    except ValueError:
        return str(p)


def rg_search(ctx: ToolContext, pattern: str, path: str = "", context_lines: int = 3) -> str:
    """Run ripgrep with JSON args. Never passes user data through a shell."""
    if not pattern:
        return "error: pattern is required"
    context_lines = max(0, min(int(context_lines), 10))
    cmd = [
        "rg",
        "-n",
        "-C",
        str(context_lines),
        "-m",
        str(MAX_RG_RESULTS),
        "--with-filename",
        "--no-heading",
        "--",
        pattern,
    ]
    if path:
        target = ctx._resolve_in_scope(path)
        cmd.append(str(target))
    elif ctx.scope_files:
        cmd.extend(str(p) for p in ctx.scope_files)
    else:
        cmd.append(str(ctx.corpus_dir))

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except FileNotFoundError:
        return "error: ripgrep (rg) is not installed"
    except subprocess.TimeoutExpired:
        return "error: rg timed out after 30s"

    out = proc.stdout.strip() or "(no matches)"
    if proc.returncode not in (0, 1):
        err = (proc.stderr or "").strip()
        return f"error: rg exited {proc.returncode}: {err[:1000]}"
    return ctx.truncate(out)


def list_dir(ctx: ToolContext, path: str = "", max_entries: int = 200) -> str:
    try:
        target = ctx._resolve_in_corpus(path) if path else ctx.corpus_dir.resolve()
    except ToolSecurityError as exc:
        return f"error: {exc}"
    if target.is_file():
        return f"{_short_path(ctx, target)}"
    if not target.is_dir():
        return "error: not found"
    entries = sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name))
    max_entries = max(1, min(int(max_entries), 500))
    lines = []
    for p in entries[:max_entries]:
        kind = "dir" if p.is_dir() else "file"
        try:
            size = p.stat().st_size if p.is_file() else 0
        except OSError:
            size = 0
        lines.append(f"{kind:4s} {size:>8d}  {_short_path(ctx, p)}")
    return ctx.truncate("\n".join(lines) or "(empty)")


def read_file(ctx: ToolContext, path: str, start_line: int = 1, end_line: int = 200) -> str:
    try:
        target = ctx._resolve_in_scope(path)
        if not target.is_file():
            return f"error: not a file: {path}"
        start_line = max(1, int(start_line))
        end_line = max(start_line, int(end_line))
        if end_line - start_line + 1 > MAX_READ_LINES:
            end_line = start_line + MAX_READ_LINES - 1
        lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
        out = []
        for i in range(start_line - 1, min(end_line, len(lines))):
            out.append(f"{i + 1}: {lines[i]}")
        if not out:
            return "(line range out of bounds)"
        return ctx.truncate("\n".join(out))
    except (OSError, UnicodeError, ToolSecurityError) as exc:
        return f"error: {exc}"


def sqlite_query(ctx: ToolContext, sql: str) -> str:
    """Run one read-only SELECT/WITH query against papers.db."""
    sql = (sql or "").strip().rstrip(";").strip()
    if not SELECT_RE.match(sql):
        return "error: only SELECT/WITH queries are allowed"
    if re.search(r";\s*\S", sql):
        return "error: multiple statements are not allowed"
    try:
        uri = f"file:{ctx.db_path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        cur = conn.execute(sql)
        rows = cur.fetchmany(MAX_SQL_ROWS + 1)
        cols = [d[0] for d in cur.description] if cur.description else []
        conn.close()
    except Exception as exc:
        return f"error: sqlite query failed: {exc}"

    out_rows = [dict(zip(cols, [r[i] for i in range(len(cols))])) for r in rows[:MAX_SQL_ROWS]]
    truncated = len(rows) > MAX_SQL_ROWS
    payload = {"columns": cols, "rows": out_rows, "truncated": truncated}
    return ctx.truncate(json.dumps(payload, ensure_ascii=False, default=str))


TOOL_SPECS = [
    {
        "name": "rg",
        "description": "Regex search in the paper corpus. Returns file_path:line_number plus surrounding context. Use multiple targeted patterns before reading a file.",
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "ripgrep regex, e.g. GraphRAG|KBQA"},
                "path": {"type": "string", "description": "Optional file or directory path. Empty means all files in the current question scope."},
                "context_lines": {"type": "integer", "description": "Lines of context around each match, default 3, max 10."},
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "read_file",
        "description": "Read a numbered line range from one Markdown file in the corpus.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "start_line": {"type": "integer"},
                "end_line": {"type": "integer"},
            },
            "required": ["path", "start_line", "end_line"],
        },
    },
    {
        "name": "list_dir",
        "description": "List files/directories under the corpus root.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "max_entries": {"type": "integer"},
            },
            "required": [],
        },
    },
    {
        "name": "sqlite_query",
        "description": "Run one read-only SQL query on papers metadata. Tables: papers(id,title,title_zh,abstract,abstract_zh,authors,year,venue,url,pdf_url,doi,tags,status,pdf_status,parse_status,translate_status,md_path,md_zh_path). tags is a JSON array string.",
        "parameters": {
            "type": "object",
            "properties": {"sql": {"type": "string", "description": "One SELECT or WITH query."}},
            "required": ["sql"],
        },
    },
]


def execute_tool(name: str, arguments: dict, ctx: ToolContext) -> str:
    args = arguments or {}
    if name == "rg":
        return rg_search(ctx, str(args.get("pattern", "")), str(args.get("path", "")), int(args.get("context_lines", 3)))
    if name == "read_file":
        return read_file(ctx, str(args.get("path", "")), int(args.get("start_line", 1)), int(args.get("end_line", 200)))
    if name == "list_dir":
        return list_dir(ctx, str(args.get("path", "")), int(args.get("max_entries", 200)))
    if name == "sqlite_query":
        return sqlite_query(ctx, str(args.get("sql", "")))
    return f"error: unknown tool {name!r}"


__all__ = [
    "ToolContext",
    "ToolSecurityError",
    "TOOL_SPECS",
    "execute_tool",
    "rg_search",
    "read_file",
    "list_dir",
    "sqlite_query",
]
