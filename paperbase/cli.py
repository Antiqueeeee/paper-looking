"""Minimal CLI. Phase 1 exposes init/fetch; later phases add today/ask/read."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from paperbase.config import load_config
from paperbase.db import init_db, utcnow
from paperbase.paths import PaperPaths
from paperbase.pipeline.digest import build_daily_digest, queue_papers
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

    p_today = sub.add_parser("today", help="generate/print today's digest")
    p_today.add_argument("--no-translate", action="store_true", help="skip metadata translation")
    p_today.set_defaults(func=cmd_today)

    p_queue = sub.add_parser("queue", help="add/remove papers to/from reading queue")
    p_queue.add_argument("ids", nargs="+", help="paper ids")
    p_queue.add_argument("--remove", action="store_true", help="remove from queue")
    p_queue.set_defaults(func=cmd_queue)
    return parser


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
