"""Daily scheduler and task consumer for the single worker process."""
from __future__ import annotations

import logging
import time
from paperbase.alerts import alert_once_daily
from paperbase.db import init_db
from paperbase.paths import PaperPaths
from paperbase.pipeline.handlers import process_task
from paperbase.storage import disk_usage_ratio
from paperbase.tasks import claim_next_task, reset_running_tasks

logger = logging.getLogger(__name__)


def run_daily_pipeline(conn, config: dict, paths: PaperPaths, *, translate: bool = True) -> dict:
    """Fetch all configured sources, then build the daily digest.

    Digest translation failures do not prevent the digest itself from being
    produced; the digest falls back to untranslated titles/abstracts.
    """
    from paperbase.sources import fetch_source
    from paperbase.pipeline.digest import build_daily_digest

    fetch_results = []
    fetch_errors = 0
    for source_name in config.get("fetch", {}).get("sources", []):
        try:
            report = fetch_source(conn, config, source_name)
            fetch_results.append(report)
            if report.status != "success":
                fetch_errors += 1
        except Exception as exc:
            fetch_errors += 1
            fetch_results.append({"source": source_name, "status": "failed", "message": str(exc)})

    if fetch_errors:
        alert_once_daily(
            conn, config, "fetch_failed",
            "抓取任务部分失败",
            f"{fetch_errors} 个数据源失败：" + json_dumps(fetch_results, ensure_ascii=False),
        )

    try:
        digest = build_daily_digest(conn, config, paths, translate=translate, write_file=True)
    except Exception as exc:
        logger.warning("digest with translation failed (%s); generating untranslated digest", exc)
        digest = build_daily_digest(conn, config, paths, translate=False, write_file=True)

    return {
        "fetch": [getattr(r, "__dict__", r) for r in fetch_results],
        "digest": digest.__dict__ if hasattr(digest, "__dict__") else str(digest),
    }


def json_dumps(obj, **kwargs) -> str:
    import json

    return json.dumps(obj, default=str, **kwargs)


def check_disk_policy(conn, config: dict, paths: PaperPaths) -> str:
    """Return 'ok', 'warn' or 'block' based on disk usage thresholds."""
    storage_cfg = config.get("storage", {})
    warn = float(storage_cfg.get("disk_warn_ratio", 0.80))
    block = float(storage_cfg.get("disk_block_ratio", 0.90))
    ratio = disk_usage_ratio(paths.root)
    if ratio >= block:
        alert_once_daily(conn, config, "disk_block", "磁盘空间不足", f"磁盘使用率 {ratio:.1%}")
        return "block"
    if ratio >= warn:
        alert_once_daily(conn, config, "disk_warn", "磁盘空间告警", f"磁盘使用率 {ratio:.1%}")
        return "warn"
    return "ok"


BLOCKED_TASK_TYPES_AT_90 = {"download_pdf", "translate_full"}


def run_task_loop(conn, config: dict, paths: PaperPaths, *, once: bool = False, task_type: str | None = None) -> int:
    """Claim and process queued tasks until empty or `once` is true.

    At the 90% disk threshold, download_pdf and translate_full tasks are
    paused; parse tasks (which only grow Markdown by a small amount) continue.
    """
    reset_running_tasks(conn)
    processed = 0
    all_types = ["download_pdf", "parse_pdf", "translate_full", "translate_meta"]
    while True:
        policy = check_disk_policy(conn, config, paths)
        types = [task_type] if task_type else all_types
        if policy == "block":
            types = [t for t in types if t not in BLOCKED_TASK_TYPES_AT_90]
            if not types:
                logger.warning("all task types paused by disk policy; worker idling")
                return processed

        claimed = None
        for t in types:
            claimed = claim_next_task(conn, task_type=t, limit=1)
            if claimed:
                break
        if not claimed:
            return processed
        task = claimed[0]
        logger.info("processing task %s %s for %s", task["id"], task["task_type"], task["paper_id"])
        process_task(conn, config, paths, task)
        processed += 1

        # Budget blocks release the task back to queued without consuming an
        # attempt. Do not busy-loop on it; the next scheduler tick will retry.
        row = conn.execute("SELECT status, last_error FROM tasks WHERE id=?", (task["id"],)).fetchone()
        if row and row["status"] == "queued" and "budget exhausted" in (row["last_error"] or ""):
            logger.warning("task %s released by budget policy; pausing consumer", task["id"])
            return processed

        if once:
            return processed
        time.sleep(0.05)


def build_scheduler(config: dict, conn, paths: PaperPaths):
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger

    scheduler = BackgroundScheduler()
    schedule = str(config.get("fetch", {}).get("schedule", "07:30"))
    if ":" in schedule:
        hour, minute = schedule.split(":", 1)
        trigger = CronTrigger(hour=int(hour), minute=int(minute))
    else:
        trigger = CronTrigger.from_crontab(schedule)

    def daily():
        logger.info("daily pipeline started")
        try:
            run_daily_pipeline(conn, config, paths)
        except Exception:
            logger.exception("daily pipeline failed")
        try:
            run_task_loop(conn, config, paths)
        except Exception:
            logger.exception("task loop failed")

    scheduler.add_job(daily, trigger=trigger, id="daily", max_instances=1, coalesce=True)
    scheduler.add_job(
        lambda: run_task_loop(conn, config, paths),
        "interval",
        minutes=5,
        id="tasks",
        max_instances=1,
        coalesce=True,
    )
    return scheduler


def main_loop(config: dict, paths: PaperPaths) -> None:
    conn = init_db(paths.db_path)
    scheduler = build_scheduler(config, conn, paths)
    scheduler.start()
    logger.info("worker started: daily=%s db=%s", config.get("fetch", {}).get("schedule"), paths.db_path)
    try:
        while True:
            time.sleep(3600)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown(wait=False)
        conn.close()


__all__ = [
    "run_daily_pipeline",
    "run_task_loop",
    "check_disk_policy",
    "build_scheduler",
    "main_loop",
]
