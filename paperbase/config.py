"""Configuration loading with sane defaults.

All thresholds in BDD.md are represented here with their default values.
`config.example.toml` documents every supported key.
"""
from __future__ import annotations

import os
import tomllib
from copy import deepcopy
from pathlib import Path
from typing import Any

DEFAULTS: dict[str, Any] = {
    "paths": {
        "data_dir": "./data",
    },
    "fetch": {
        "schedule": "07:30",
        "sources": ["acl", "openalex"],
        "years": [2024, 2025, 2026],
        "concurrency": 4,
    },
    "digest": {
        "due_time": "08:00",
        "top_n_per_tag": 30,
    },
    "llm": {
        "base_url": "https://api.openai.com/v1",
        "api_key_env": "OPENAI_API_KEY",
        "model": "gpt-4o-mini",
        "reasoning_model": "",
        "timeout_seconds": 120,
        "max_retries": 2,
    },
    "translation": {
        "meta_batch_size": 5,
        "full_chunk_min_chars": 4000,
        "full_chunk_max_chars": 8000,
        "keep_terms": ["GraphRAG", "KBQA", "RAG", "entity alignment", "LLM"],
    },
    "mineru": {
        "base_url": "",
        "api_key_env": "MINERU_API_KEY",
        "poll_interval_seconds": 5,
        "max_poll_seconds": 3600,
        "max_concurrent": 2,
        "min_md_chars": 200,
    },
    "pdf": {
        "max_upload_mb": 100,
        "download_timeout_seconds": 120,
        "download_retries": 2,
        "hot_quota_gb": 6,
    },
    "storage": {
        "cache_quota_gb": 1,
        "disk_warn_ratio": 0.80,
        "disk_block_ratio": 0.90,
        "object_store": "filesystem",
        "object_root": "./data/cold",
        "endpoint_url": "",
        "bucket": "",
        "access_key_env": "S3_ACCESS_KEY",
        "secret_key_env": "S3_SECRET_KEY",
    },
    "dci": {
        "max_tool_calls": 30,
        "tool_output_chars": 12000,
        "corpus_readonly": True,
    },
    "budgets": {
        "translate_daily_tokens": 5_000_000,
        "translate_monthly_tokens": 100_000_000,
        "qa_daily_tokens": 1_000_000,
        "parse_daily_count": 50,
    },
    "alerts": {
        "webhook_url": "",
        "consecutive_fetch_failures": 3,
    },
    "access": {
        "token": "",
        "bind_host": "127.0.0.1",
        "bind_port": 8000,
    },
}


def deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge `override` into a copy of `base`."""
    out = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config(path: str | Path | None = None) -> dict:
    """Load TOML config and merge it over defaults.

    `PAPERBASE_CONFIG` env var is used when `path` is not provided.
    Missing keys fall back to defaults, so partial configs are valid.
    """
    config = deepcopy(DEFAULTS)
    if path is None:
        env_path = os.environ.get("PAPERBASE_CONFIG")
        if not env_path:
            return config
        path = env_path
    p = Path(path).expanduser()
    if p.exists():
        with open(p, "rb") as f:
            user_cfg = tomllib.load(f)
        config = deep_merge(config, user_cfg)
    return config


def data_dir(config: dict) -> Path:
    return Path(config["paths"]["data_dir"]).expanduser().resolve()


__all__ = ["DEFAULTS", "load_config", "deep_merge", "data_dir"]
