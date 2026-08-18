"""Daily scheduler and task consumer for the single worker process."""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from paperbase.alerts import alert_once_daily
from paperbase.config import database_target
from paperbase.db import init_db
from paperbase.paths import PaperPaths
from paperbase.pipeline.handlers import process_task
from paperbase.storage import disk_usage_ratio, prune_cache
from paperbase.tasks import claim_next_task, fail_task, reset_running_tasks

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
    cache_quota = int(float(config.get("storage", {}).get("cache_quota_gb", 1)) * 1024 ** 3)
    prune_cache(paths.cache_dir, cache_quota)
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
        try:
            process_task(conn, config, paths, task)
        except Exception:
            logger.exception("task handler crashed for %s", task["id"])
            fail_task(conn, int(task["id"]), "handler crashed; see worker log")
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


def _cron_field_values(field: str, lo: int, hi: int) -> set[int]:
    """Expand one cron field: * , list, range and step."""
    out: set[int] = set()
    for part in str(field).split(","):
        part = part.strip()
        if not part:
            continue
        step = 1
        if "/" in part:
            part, step_s = part.split("/", 1)
            step = int(step_s or 1)
        if part in ("*", ""):
            vals = range(lo, hi + 1)
        elif "-" in part:
            a, b = part.split("-", 1)
            vals = range(int(a), int(b) + 1)
        else:
            vals = [int(part)]
        for v in vals:
            if lo <= v <= hi and (v - lo) % step == 0:
                out.add(v)
    return out


def parse_schedule(schedule: str) -> dict:
    """Return {'kind': 'daily'|'cron', ...} for worker scheduling."""
    schedule = str(schedule or "0 2 * * 1").strip()
    if ":" in schedule and len(schedule.split()) == 1:
        hour, minute = schedule.split(":", 1)
        return {"kind": "daily", "hour": int(hour), "minute": int(minute)}
    fields = schedule.split()
    if len(fields) == 5:
        return {
            "kind": "cron",
            "minute": _cron_field_values(fields[0], 0, 59),
            "hour": _cron_field_values(fields[1], 0, 23),
            "day": _cron_field_values(fields[2], 1, 31),
            "month": _cron_field_values(fields[3], 1, 12),
            "dow": _cron_field_values(fields[4], 0, 7),  # 0 and 7 are Sunday
        }
    raise ValueError(f"unsupported schedule: {schedule!r}")


class _FallbackScheduler:
    """Minimal standard-library scheduler used when APScheduler is absent."""

    def __init__(self, conn, config: dict, paths: PaperPaths):
        self.conn = conn
        self.config = config
        self.paths = paths
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.schedule = parse_schedule(str(config.get("fetch", {}).get("schedule", "0 2 * * 1")))

    def _matches(self, now: datetime) -> bool:
        if self.schedule["kind"] == "daily":
            return now.hour == self.schedule["hour"] and now.minute == self.schedule["minute"]
        # cron: 0/7=Sunday, 1=Monday ... 6=Saturday; datetime: Monday=0.
        python_dows = {0 if d in (0, 7) else d - 1 for d in self.schedule["dow"]}
        return (
            now.minute in self.schedule["minute"]
            and now.hour in self.schedule["hour"]
            and now.day in self.schedule["day"]
            and now.month in self.schedule["month"]
            and now.weekday() in python_dows
        )

    def _daily(self):
        logger.info("daily pipeline started (fallback scheduler)")
        try:
            run_daily_pipeline(self.conn, self.config, self.paths)
        except Exception:
            logger.exception("daily pipeline failed")
        try:
            run_task_loop(self.conn, self.config, self.paths)
        except Exception:
            logger.exception("task loop failed")

    def _tasks(self):
        try:
            run_task_loop(self.conn, self.config, self.paths)
        except Exception:
            logger.exception("task loop failed")

    def _run(self):
        last_daily_minute = None
        last_tasks = 0.0
        while not self._stop.wait(20):
            now = datetime.now()
            due_key = now.strftime("%Y-%m-%d %H:%M")
            if last_daily_minute != due_key and self._matches(now):
                last_daily_minute = due_key
                self._daily()
                last_tasks = time.time()
            elif time.time() - last_tasks >= 300:
                last_tasks = time.time()
                self._tasks()

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="paper-worker", daemon=True)
            self._thread.start()

    def shutdown(self, wait: bool = False):
        self._stop.set()
        if wait and self._thread:
            self._thread.join(timeout=5)

    def get_jobs(self):
        return [{"id": "daily"}, {"id": "tasks"}]


def build_scheduler(config: dict, conn, paths: PaperPaths):
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger
    except ImportError:
        logger.warning("APScheduler not installed; using standard-library fallback scheduler")
        return _FallbackScheduler(conn, config, paths)

    scheduler = BackgroundScheduler()
    schedule = str(config.get("fetch", {}).get("schedule", "0 2 * * 1"))
    parsed = parse_schedule(schedule)
    if parsed["kind"] == "daily":
        trigger = CronTrigger(hour=parsed["hour"], minute=parsed["minute"])
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
    conn = init_db(database_target(config, paths.db_path))
    # Drain anything queued before waiting for the next scheduler tick.
    try:
        processed = run_task_loop(conn, config, paths)
        if processed:
            logger.info("startup task drain processed %s task(s)", processed)
    except Exception:
        logger.exception("startup task drain failed")
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
    "parse_schedule",
    "build_scheduler",
    "main_loop",
]
