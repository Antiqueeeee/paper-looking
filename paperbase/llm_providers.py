"""LLM provider registry.

All supported providers speak the OpenAI chat-completions dialect, so the
runtime client stays the same; only base_url / key env / model defaults change.

Supported presets:
  * deepseek   DeepSeek official API
  * openai     OpenAI
  * dashscope  Aliyun Bailian (Qwen) OpenAI-compatible endpoint
  * zhipu      Zhipu GLM
  * moonshot   Moonshot Kimi
  * local      Ollama / vLLM local OpenAI-compatible server
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from paperbase.llm import OpenAICompatibleClient


@dataclass(frozen=True)
class ProviderPreset:
    name: str
    base_url: str
    api_key_env: str
    default_model: str
    reasoning_model: str = ""
    api_key_required: bool = True


PRESETS: dict[str, ProviderPreset] = {
    "deepseek": ProviderPreset(
        "deepseek",
        "https://api.deepseek.com/v1",
        "DEEPSEEK_API_KEY",
        "deepseek-chat",
        "deepseek-reasoner",
    ),
    "openai": ProviderPreset(
        "openai",
        "https://api.openai.com/v1",
        "OPENAI_API_KEY",
        "gpt-4o-mini",
    ),
    "dashscope": ProviderPreset(
        "dashscope",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "DASHSCOPE_API_KEY",
        "qwen-plus",
    ),
    "zhipu": ProviderPreset(
        "zhipu",
        "https://open.bigmodel.cn/api/paas/v4",
        "ZHIPU_API_KEY",
        "glm-4-flash",
    ),
    "moonshot": ProviderPreset(
        "moonshot",
        "https://api.moonshot.cn/v1",
        "MOONSHOT_API_KEY",
        "moonshot-v1-8k",
    ),
    "local": ProviderPreset(
        "local",
        "http://127.0.0.1:11434/v1",
        "",
        "qwen2.5:14b",
        api_key_required=False,
    ),
}


def resolve_llm_config(config: dict) -> dict:
    """Merge provider preset with user overrides.

    Resolution order (later wins):
      preset defaults -> [llm] section -> [providers.<name>] section
    """
    llm = dict(config.get("llm", {}) or {})
    provider_name = str(llm.get("provider") or "deepseek")
    preset = PRESETS.get(provider_name)
    if preset is None:
        raise ValueError(
            f"unknown LLM provider {provider_name!r}; available={sorted(PRESETS)}"
        )

    out = {
        "provider": provider_name,
        "base_url": preset.base_url,
        "api_key_env": preset.api_key_env,
        "model": preset.default_model,
        "reasoning_model": preset.reasoning_model,
        "api_key_required": preset.api_key_required,
    }
    out.update({k: v for k, v in llm.items() if v not in ("", None)})
    provider_cfg = config.get("providers", {}).get(provider_name, {}) or {}
    out.update({k: v for k, v in provider_cfg.items() if v not in ("", None)})
    return out


def build_llm_client(config: dict, conn=None):
    """Build an OpenAI-compatible client for the selected provider."""
    llm_cfg = resolve_llm_config(config)
    api_key = os.environ.get(llm_cfg.get("api_key_env", ""), "")
    if not api_key:
        if llm_cfg.get("api_key_required", True):
            env_name = llm_cfg.get("api_key_env", "DEEPSEEK_API_KEY")
            raise RuntimeError(f"LLM API key not set: export {env_name}")
        api_key = "local-no-auth"

    from paperbase.pipeline.translate import make_cost_logger  # local import avoids cycle

    return OpenAICompatibleClient(
        base_url=str(llm_cfg.get("base_url")),
        api_key=api_key,
        model=str(llm_cfg.get("model")),
        timeout_seconds=float(llm_cfg.get("timeout_seconds", 120)),
        max_retries=int(llm_cfg.get("max_retries", 2)),
        cost_callback=make_cost_logger(conn) if conn is not None else None,
    )


__all__ = ["PRESETS", "ProviderPreset", "resolve_llm_config", "build_llm_client"]
