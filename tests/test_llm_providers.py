"""Provider registry tests."""
from __future__ import annotations

import pytest

from paperbase.config import load_config
from paperbase.llm_providers import PRESETS, build_llm_client, resolve_llm_config


def test_default_provider_is_deepseek():
    cfg = load_config()
    assert cfg["llm"]["provider"] == "deepseek"
    resolved = resolve_llm_config(cfg)
    assert resolved["base_url"] == "https://api.deepseek.com/v1"
    assert resolved["model"] == "deepseek-chat"
    assert resolved["api_key_env"] == "DEEPSEEK_API_KEY"


def test_provider_override_by_name():
    cfg = {
        "llm": {"provider": "local", "base_url": "http://127.0.0.1:11434/v1", "model": "qwen2.5:7b"},
        "budgets": {},
    }
    resolved = resolve_llm_config(cfg)
    assert resolved["api_key_required"] is False
    client = build_llm_client(cfg)
    assert client.model == "qwen2.5:7b"
    assert client.api_key == "local-no-auth"


def test_provider_specific_section_wins():
    cfg = {
        "llm": {"provider": "dashscope"},
        "providers": {"dashscope": {"model": "qwen-max"}},
    }
    assert resolve_llm_config(cfg)["model"] == "qwen-max"


def test_unknown_provider_raises():
    with pytest.raises(ValueError):
        resolve_llm_config({"llm": {"provider": "nope"}})
