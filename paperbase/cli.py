"""Minimal CLI. Phase 1 exposes init/fetch; later phases add today/ask/read."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from paperbase.config import load_config
from paperbase.dci.agent import DCIQAAgent
from paperbase.db import init_db, utcnow
from paperbase.paths import PaperPaths
from paperbase.pipeline.digest import build_daily_digest, queue_papers
from paperbase.pipeline.filter import apply_rules
from paperbase.pipeline.pdf import ingest_uploaded_pdf
from paperbase.sources import fetch_source
from paperbase.sources.import_legacy import import_legacy


def _open_db(args):
    config = load_config(args.config)
    paths = PaperPaths(config["paths"]["data_dir"])
    paths.ensure_dirs()
    conn = init_db(paths.db_path)
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
