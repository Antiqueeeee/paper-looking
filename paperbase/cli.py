"""Minimal CLI. Phase 1 exposes init/fetch; later phases add today/ask/read."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from paperbase.config import database_target, load_config
from paperbase.dci.agent import DCIQAAgent
from paperbase.db import get_paper, init_db, utcnow
from paperbase.paths import PaperPaths
from paperbase.pipeline.digest import build_daily_digest, queue_papers
from paperbase.pipeline.filter import apply_rules
from paperbase.pipeline.handlers import process_task
from paperbase.pipeline.pdf import ingest_uploaded_pdf
from paperbase.sources import fetch_source
from paperbase.sources.import_legacy import import_legacy
from paperbase.sources.import_title_translations import import_title_translations


def _open_db(args):
    config = load_config(args.config)
    paths = PaperPaths(config["paths"]["data_dir"])
    paths.ensure_dirs()
    conn = init_db(database_target(config, paths.db_path))
    return config, paths, conn


def cmd_init(args) -> int:
    config, paths, conn = _open_db(args)
    legacy_dir = args.legacy_dir or str(Path("ACL-Anthology-Crawler") / "data")
    error_log = args.error_log or str(paths.root / "import_errors.log")
    report = import_legacy(conn, legacy_dir, error_log=error_log)
    print(
        f"legacy import: files={report.files} rows={report.rows} "
        f"imported={report.imported} skipped={report.skipped} errors={len(report.errors)}"
    )
    if report.errors:
        print(f"first errors (see {error_log}):")
        for line in report.errors[:10]:
            print("  -", line)
    title_report = import_title_translations(conn, legacy_dir)
    print(
        f"legacy title zh: found={title_report.found} updated={title_report.updated} "
        f"existing={title_report.skipped_existing} missing={title_report.skipped_missing_paper}"
    )
    if report.imported:
        changed = apply_rules(conn)
        print(f"interest rules applied: {changed} papers tagged")
    return 0 if report.imported else 1


def cmd_fetch(args) -> int:
    config, paths, conn = _open_db(args)
    sources = args.sources or config["fetch"]["sources"]
    rc = 0
    for name in sources:
        print(f"fetching source: {name} ...")
        report = fetch_source(conn, config, name)
        print(
            f"  {report.source}: {report.status}, drafts={report.drafts}, "
            f"papers {report.before} -> {report.after}, errors={len(report.errors)}"
        )
        if report.status == "failed":
            print("  error:", report.message)
            rc = 1
        for err in report.errors[:5]:
            print("  -", err)
    return rc


def cmd_scan(args) -> int:
    _, _, conn = _open_db(args)
    changed = apply_rules(conn, args.ids)
    print(f"tags updated: {changed} papers")
    return 0


def cmd_interest(args) -> int:
    config, _, conn = _open_db(args)
    from paperbase.interest import classify_database, profile_from_config

    profile = profile_from_config(config, args.profile)
    client = None
    if profile.llm_enabled:
        from paperbase.pipeline.translate import make_llm_client

        client = make_llm_client(config, conn)
    decisions = classify_database(conn, config, profile_id=profile.id, paper_ids=args.ids, client=client)
    counts = {}
    for decision in decisions:
        counts[decision.label] = counts.get(decision.label, 0) + 1
    print(f"classified: {len(decisions)} papers, profile={profile.id}, labels={counts}")
    return 0


def cmd_today(args) -> int:
    config, paths, conn = _open_db(args)
    client = None
    if args.no_translate:
        translate = False
    else:
        translate = True
    result = build_daily_digest(conn, config, paths, client=client, translate=translate)
    print(result.message)
    if result.path:
        print("digest:", result.path)
    elif result.baseline:
        print("提示：后续每日新增论文会从今天之后开始纳入早报。")
    return 0


def cmd_queue(args) -> int:
    _, _, conn = _open_db(args)
    if args.remove:
        ids = args.ids
        if ids:
            placeholders = ",".join("?" for _ in ids)
            conn.execute(
                f"UPDATE papers SET status='new', updated_at=? WHERE id IN ({placeholders})",
                (utcnow(), *ids),
            )
            conn.commit()
        print(f"removed from queue: {len(ids)}")
        return 0
    changed = queue_papers(conn, args.ids)
    print(f"queued: {changed} papers")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="paper", description="personal paper library")
    parser.add_argument("--config", help="TOML config path (or PAPERBASE_CONFIG)")
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="initialize DB and import legacy JSONL")
    p_init.add_argument("--legacy-dir")
    p_init.add_argument("--error-log")
    p_init.set_defaults(func=cmd_init)

    p_fetch = sub.add_parser("fetch", help="incrementally fetch one or more sources")
    p_fetch.add_argument("--sources", nargs="+", choices=["acl", "openalex", "arxiv"])
    p_fetch.add_argument("--since", help="ISO datetime lower bound (optional)")
    p_fetch.set_defaults(func=cmd_fetch)

    p_scan = sub.add_parser("scan", help="run interest rules against papers")
    p_scan.add_argument("--ids", nargs="+", help="only these paper ids")
    p_scan.set_defaults(func=cmd_scan)

    p_interest = sub.add_parser("interest", help="classify papers for a configurable interest profile")
    p_interest.add_argument("--profile", help="profile id from [interest.profiles]")
    p_interest.add_argument("--ids", nargs="+", help="only these paper ids")
    p_interest.set_defaults(func=cmd_interest)

    p_today = sub.add_parser("today", help="generate/print today's digest")
    p_today.add_argument("--no-translate", action="store_true", help="skip metadata translation")
    p_today.set_defaults(func=cmd_today)

    p_queue = sub.add_parser("queue", help="add/remove papers to/from reading queue")
    p_queue.add_argument("ids", nargs="+", help="paper ids")
    p_queue.add_argument("--remove", action="store_true", help="remove from queue")
    p_queue.set_defaults(func=cmd_queue)

    p_upload = sub.add_parser("upload", help="manually upload one or more PDFs")
    p_upload.add_argument("files", nargs="+")
    p_upload.add_argument("--paper-id", help="attach to an existing paper")
    p_upload.add_argument("--title", help="title for a new manual record")
    p_upload.set_defaults(func=cmd_upload)

    p_worker = sub.add_parser("worker", help="run queued pipeline tasks")
    p_worker.add_argument("--once", action="store_true", help="claim and run one task, then exit")
    p_worker.add_argument("--loop", action="store_true", help="run scheduler + task loop forever")
    p_worker.add_argument("--daily", action="store_true", help="run one daily pipeline now")
    p_worker.add_argument("--task-type", choices=["download_pdf", "parse_pdf", "translate_full", "translate_meta"])
    p_worker.set_defaults(func=cmd_worker)

    p_ask = sub.add_parser("ask", help="ask the DCI agent")
    p_ask.add_argument("question")
    p_ask.add_argument("--paper", help="single paper id")
    p_ask.add_argument("--papers", nargs="+", help="compare multiple paper ids")
    p_ask.set_defaults(func=cmd_ask)

    p_web = sub.add_parser("web", help="start the local web server")
    p_web.add_argument("--host", default=None)
    p_web.add_argument("--port", type=int, default=None)
    p_web.set_defaults(func=cmd_web)

    p_reparse = sub.add_parser("reparse", help="force MinerU re-parse of one paper")
    p_reparse.add_argument("paper_id")
    p_reparse.set_defaults(func=cmd_reparse)

    p_read = sub.add_parser("read", help="show paper metadata and first lines of markdown")
    p_read.add_argument("paper_id")
    p_read.add_argument("--lines", type=int, default=80)
    p_read.add_argument("--lang", choices=["en", "zh"], default="en")
    p_read.set_defaults(func=cmd_read)

    p_stats = sub.add_parser("stats", help="library statistics")
    p_stats.set_defaults(func=cmd_stats)

    p_doctor = sub.add_parser("doctor", help="environment and data checks")
    p_doctor.set_defaults(func=cmd_doctor)
    return parser


def cmd_upload(args) -> int:
    config, paths, conn = _open_db(args)
    for file_path in args.files:
        paper = ingest_uploaded_pdf(
            conn, paths, config,
            file_path,
            paper_id=args.paper_id,
            title=args.title,
        )
        print(f"uploaded: {file_path} -> {paper['id']} ({paper['title']})")
    return 0


def cmd_worker(args) -> int:
    config, paths, conn = _open_db(args)
    from paperbase.pipeline import worker as worker_mod

    if args.daily:
        report = worker_mod.run_daily_pipeline(conn, config, paths)
        import json as _json
        print(_json.dumps(report, ensure_ascii=False, default=str)[:6000])
        drained = worker_mod.run_task_loop(conn, config, paths)
        print(f"processed {drained} queued task(s)")
        return 0
    if args.loop:
        worker_mod.main_loop(config, paths)
        return 0
    done = worker_mod.run_task_loop(conn, config, paths, once=args.once, task_type=args.task_type)
    print(f"processed {done} task(s)")
    return 0


def cmd_ask(args) -> int:
    config, paths, conn = _open_db(args)
    if args.paper:
        mode, ids = "paper", [args.paper]
    elif args.papers:
        mode, ids = "compare", args.papers
    else:
        mode, ids = "library", []
    answer = DCIQAAgent(conn, config, paths).ask(args.question, mode=mode, paper_ids=ids)
    print(answer.answer)
    if answer.citations:
        print("\n引用：")
        for c in answer.citations:
            print("  " + c)
    print(f"\nconfidence={answer.confidence} tool_calls={answer.tool_calls} tokens={answer.prompt_tokens + answer.completion_tokens}")
    return 0 if answer.answer else 1


def cmd_web(args) -> int:
    config = load_config(args.config)
    access = config.get("access", {})
    import uvicorn

    from paperbase.web.app import create_app

    app = create_app(args.config)
    uvicorn.run(
        app,
        host=args.host or access.get("bind_host", "127.0.0.1"),
        port=int(args.port or access.get("bind_port", 8000)),
        workers=1,
    )
    return 0


def cmd_read(args) -> int:
    _, paths, conn = _open_db(args)
    paper = get_paper(conn, args.paper_id)
    if not paper:
        print(f"paper not found: {args.paper_id}")
        return 1
    print(f"ID:       {paper['id']}")
    print(f"标题:     {paper['title']}")
    if paper["title_zh"]:
        print(f"中文标题: {paper['title_zh']}")
    print(f"年份/来源: {paper['year']} / {paper['venue']} ({paper['source']})")
    print(f"状态:     {paper['status']}  pdf={paper['pdf_status']}  parse={paper['parse_status']}  translate={paper['translate_status']}")
    if paper["local_pdf"]:
        print(f"PDF:      {paper['local_pdf']}")
    path_key = "md_zh_path" if args.lang == "zh" else "md_path"
    md_path = paper.get(path_key) or paper.get("md_path")
    if md_path and Path(md_path).exists():
        print(f"Markdown: {md_path}")
        lines = Path(md_path).read_text(encoding="utf-8", errors="replace").splitlines()
        print("--- first lines ---")
        for line in lines[: max(1, args.lines)]:
            print(line)
    else:
        print("Markdown: 尚未解析或指定语言版本不存在")
    return 0


def cmd_stats(args) -> int:
    _, _, conn = _open_db(args)
    total = conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
    print(f"papers: {total}")
    for label, sql in [
        ("source", "SELECT source k, COUNT(*) n FROM papers GROUP BY source ORDER BY n DESC"),
        ("status", "SELECT status k, COUNT(*) n FROM papers GROUP BY status ORDER BY n DESC"),
        ("pdf_status", "SELECT pdf_status k, COUNT(*) n FROM papers GROUP BY pdf_status ORDER BY n DESC"),
        ("parse_status", "SELECT parse_status k, COUNT(*) n FROM papers GROUP BY parse_status ORDER BY n DESC"),
        ("translate_status", "SELECT translate_status k, COUNT(*) n FROM papers GROUP BY translate_status ORDER BY n DESC"),
    ]:
        print(label + ":", ", ".join(f"{r['k']}={r['n']}" for r in conn.execute(sql).fetchall()))
    queued = conn.execute("SELECT COUNT(*) FROM tasks WHERE status='queued'").fetchone()[0]
    print(f"queued_tasks: {queued}")
    return 0


def cmd_doctor(args) -> int:
    import os
    import shutil

    config, paths, conn = _open_db(args)
    problems = []
    try:
        conn.execute("SELECT 1").fetchone()
        print(f"[ok] sqlite: {paths.db_path}")
    except Exception as exc:
        problems.append(f"sqlite: {exc}")
    if shutil.which("rg"):
        print("[ok] ripgrep installed")
    else:
        problems.append("ripgrep (rg) is not installed")
    usage = shutil.disk_usage(paths.root)
    ratio = usage.used / usage.total
    print(f"[info] disk usage {ratio:.1%}, free {usage.free/1024**3:.1f}GB")
    if ratio >= float(config.get("storage", {}).get("disk_block_ratio", 0.90)):
        problems.append(f"disk usage {ratio:.1%} above block threshold")
    for env in [config.get("llm", {}).get("api_key_env", "OPENAI_API_KEY"),
                config.get("mineru", {}).get("api_key_env", "MINERU_API_KEY")]:
        print(f"[{'ok' if os.environ.get(env) else 'warn'}] {env} is {'set' if os.environ.get(env) else 'NOT set'}")
    if problems:
        print("problems:")
        for p in problems:
            print(" -", p)
        return 1
    print("[ok] doctor checks passed")
    return 0


def cmd_reparse(args) -> int:
    from paperbase.db import get_paper as get_p
    from paperbase.tasks import content_hash, enqueue_task, task_to_dict

    config, paths, conn = _open_db(args)
    paper = get_p(conn, args.paper_id)
    if not paper:
        print(f"paper not found: {args.paper_id}")
        return 1
    pdf_path = paper.get("local_pdf") or ""
    if not Path(pdf_path).exists():
        print("local PDF not found; upload or restore it first")
        return 1
    tid = enqueue_task(
        conn,
        paper_id=paper["id"],
        task_type="parse_pdf",
        payload={"pdf_path": pdf_path},
        input_hash=content_hash("reparse", paper["id"], utcnow()),
    )
    task = task_to_dict(conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone())
    process_task(conn, config, paths, task)
    row = conn.execute("SELECT status, last_error FROM tasks WHERE id=?", (tid,)).fetchone()
    print(f"reparse task {tid}: {row['status']}")
    if row["last_error"]:
        print(row["last_error"])
    return 0 if row["status"] == "done" else 1


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
