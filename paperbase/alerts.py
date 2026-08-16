"""Lightweight alerting via webhook, with once-per-day deduplication."""
from __future__ import annotations

from datetime import datetime, timezone

import requests

from paperbase.db import get_meta, set_meta, utcnow


def send_alert(config: dict, title: str, message: str) -> bool:
    webhook = (config.get("alerts") or {}).get("webhook_url", "")
    if not webhook:
        return False
    try:
        requests.post(
            webhook,
            json={"msg_type": "text", "text": {"content": f"[PaperBase] {title}\n{message}"}},
            timeout=10,
        )
        return True
    except Exception:
        return False


def alert_once_daily(conn, config: dict, key: str, title: str, message: str) -> None:
    """Send an alert at most once per UTC day for a given key."""
    meta_key = f"alert:{key}:{datetime.now(timezone.utc).date().isoformat()}"
    if get_meta(conn, meta_key):
        return
    if send_alert(config, title, message):
        set_meta(conn, meta_key, utcnow())


__all__ = ["send_alert", "alert_once_daily"]
