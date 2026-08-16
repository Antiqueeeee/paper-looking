"""LLM client contract and OpenAI-compatible implementation.

Business code must call `LLMClient.chat()` rather than speaking HTTP itself.
The implementation below adds retries, normalized responses and cost logging.
"""
from __future__ import annotations

import time
from typing import Any, Callable, Iterable

import requests

from .models import LLMMessage, LLMResponse, LLMUsage, ToolCall

CostCallback = Callable[[str, str, LLMUsage, float], None]

# Fallback prices per 1M tokens, used only for rough estimates.
DEFAULT_PRICES = {
    "input": 0.15,
    "output": 0.60,
    "cached_input": 0.015,
}


def estimate_cost_usd(model: str, usage: LLMUsage) -> float:
    """Rough estimate; providers/agents may pass a more precise callback."""
    return (
        usage.prompt_tokens / 1_000_000 * DEFAULT_PRICES["input"]
        + usage.completion_tokens / 1_000_000 * DEFAULT_PRICES["output"]
    )


class LLMError(RuntimeError):
    pass


class OpenAICompatibleClient:
    """Minimal chat-completions client for OpenAI-compatible endpoints."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout_seconds: float = 120,
        max_retries: int = 2,
        cost_callback: CostCallback | None = None,
    ):
        base_url = base_url.rstrip("/")
        self.endpoint = base_url if base_url.endswith("/chat/completions") else base_url + "/chat/completions"
        self.api_key = api_key
        self.model = model
        self.timeout = timeout_seconds
        self.max_retries = max_retries
        self.cost_callback = cost_callback
        self.session = requests.Session()

    def _serialize_message(self, m: LLMMessage | dict) -> dict:
        if isinstance(m, dict):
            return m
        out: dict[str, Any] = {"role": m.role, "content": m.content}
        for key in ("name", "tool_calls", "tool_call_id"):
            value = getattr(m, key, None)
            if value is not None:
                out[key] = value
        return out

    def _parse_response(self, data: dict) -> LLMResponse:
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        usage = data.get("usage") or {}

        raw_tool_calls = message.get("tool_calls") or []
        tool_calls = [
            ToolCall(
                id=tc.get("id", str(i)),
                name=(tc.get("function") or {}).get("name", ""),
                arguments=(tc.get("function") or {}).get("arguments", "{}"),
            )
            for i, tc in enumerate(raw_tool_calls)
        ]
        return LLMResponse(
            content=message.get("content") or "",
            role=message.get("role", "assistant"),
            tool_calls=tool_calls,
            finish_reason=choice.get("finish_reason", "stop"),
            usage=LLMUsage(
                prompt_tokens=int(usage.get("prompt_tokens", 0)),
                completion_tokens=int(usage.get("completion_tokens", 0)),
                total_tokens=int(usage.get("total_tokens", 0)),
            ),
            raw=data,
        )

    def chat(
        self,
        messages: list[LLMMessage] | Iterable[dict],
        *,
        tools: list[dict] | None = None,
        tool_choice: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        budget_tag: str = "default",
    ) -> LLMResponse:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [self._serialize_message(m) for m in messages],
        }
        if tools:
            payload["tools"] = tools
        if tool_choice:
            payload["tool_choice"] = tool_choice
        if temperature is not None:
            payload["temperature"] = temperature
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens

        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = self.session.post(
                    self.endpoint,
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                    timeout=self.timeout,
                )
                if resp.status_code >= 400:
                    raise LLMError(f"LLM API {resp.status_code}: {resp.text[:500]}")
                result = self._parse_response(resp.json())
                if self.cost_callback:
                    self.cost_callback(
                        budget_tag, self.model, result.usage,
                        estimate_cost_usd(self.model, result.usage),
                    )
                return result
            except (requests.RequestException, LLMError, ValueError) as exc:  # noqa: PERF203
                last_error = exc
                if attempt < self.max_retries:
                    time.sleep(2 * (attempt + 1))
        raise LLMError(f"LLM request failed after retries: {last_error}") from last_error


class MockLLMClient:
    """Deterministic client for contract and integration tests."""

    def __init__(self, script: list[LLMResponse] | None = None):
        self.script = list(script or [])
        self.calls: list[dict] = []

    def chat(self, messages, **kwargs) -> LLMResponse:
        self.calls.append({"messages": list(messages), **kwargs})
        if self.script:
            return self.script.pop(0)
        return LLMResponse(content="")

    def __repr__(self) -> str:
        return f"MockLLMClient(remaining={len(self.script)})"


__all__ = [
    "LLMError",
    "OpenAICompatibleClient",
    "MockLLMClient",
    "estimate_cost_usd",
    "CostCallback",
]
