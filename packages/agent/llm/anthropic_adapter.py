"""Anthropic Claude adapter (roadmap Phase 1 scenario B).

Uses the Messages API over httpx. Adaptive thinking is requested when reasoning
is enabled; only an auditable rationale/text is returned to callers, never the
raw thinking blocks.
"""

from __future__ import annotations

import math
from typing import Any

import httpx

from packages.agent.llm.base import LLMCallMetadata, extract_json, parse_into_schema
from packages.common.errors import ValidationAppError

_DEFAULT_BASE_URL = "https://api.anthropic.com"
_ANTHROPIC_VERSION = "2023-06-01"
_JSON_SYSTEM_PROMPT = (
    "You are an SRE diagnosis assistant. Respond with a single valid JSON object "
    "or array only — no prose, no Markdown fences." 
    "最后用中文回答问题"
)


class AnthropicAdapter:
    """Adapter for the Anthropic Messages API."""

    provider = "anthropic"

    def __init__(
        self,
        *,
        model: str,
        api_key: str | None,
        base_url: str = _DEFAULT_BASE_URL,
        timeout_seconds: float = 30.0,
        max_tokens: int = 512,
        temperature: float = 0.1,
        reasoning_enabled: bool = False,
        reasoning_effort: str = "medium",
        client: httpx.Client | None = None,
    ) -> None:
        if not api_key:
            raise ValidationAppError("Anthropic provider requires llm_api_key")
        self.model = model
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.reasoning_enabled = reasoning_enabled
        self.reasoning_effort = reasoning_effort
        self._client = client
        self.last_metadata: LLMCallMetadata = {}

    def invoke(
        self, messages: list[dict[str, Any]], *, thinking: bool = False, **kwargs: Any
    ) -> str:
        text, meta = self.invoke_with_metadata(messages, thinking=thinking, **kwargs)
        self.last_metadata = meta
        return text

    def invoke_with_metadata(
        self, messages: list[dict[str, Any]], *, thinking: bool = False, **kwargs: Any
    ) -> tuple[str, LLMCallMetadata]:
        request_model = _string_option(kwargs.get("model"), self.model)
        request_max_tokens = _positive_int_option(kwargs.get("max_tokens"), self.max_tokens)
        request_temperature = _non_negative_float_option(
            kwargs.get("temperature"), self.temperature
        )
        request_reasoning_effort = _string_option(
            kwargs.get("reasoning_effort"), self.reasoning_effort
        )
        system, chat = _split_system(messages)
        payload: dict[str, Any] = {
            "model": request_model,
            "max_tokens": request_max_tokens,
            "messages": chat,
        }
        if not thinking:
            payload["temperature"] = request_temperature
        if system:
            payload["system"] = system
        if thinking:
            payload["thinking"] = {"type": "adaptive"}
            payload["output_config"] = {"effort": request_reasoning_effort}
        data = self._post("/v1/messages", payload)
        text = _extract_text(data)
        meta = self._metadata_from_response(data, request_model=request_model)
        return text, dict(meta)

    def generate_json(
        self, prompt: str, output_schema: Any, *, thinking: bool = False, **kwargs: Any
    ) -> Any:
        result, meta = self.generate_json_with_metadata(
            prompt, output_schema, thinking=thinking, **kwargs
        )
        self.last_metadata = meta
        return result

    def generate_json_with_metadata(
        self, prompt: str, output_schema: Any, *, thinking: bool = False, **kwargs: Any
    ) -> tuple[Any, LLMCallMetadata]:
        messages = [
            {"role": "system", "content": _JSON_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ]
        raw, meta = self.invoke_with_metadata(messages, thinking=thinking, **kwargs)
        return parse_into_schema(extract_json(raw), output_schema), meta

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": _ANTHROPIC_VERSION,
            "content-type": "application/json",
        }
        if self._client is not None:
            response = self._client.post(
                path, json=payload, headers=headers, timeout=self.timeout_seconds
            )
        else:
            with httpx.Client(base_url=self.base_url, timeout=self.timeout_seconds) as client:
                response = client.post(path, json=payload, headers=headers)
        response.raise_for_status()
        result: dict[str, Any] = response.json()
        return result

    def _metadata_from_response(
        self, data: dict[str, Any], *, request_model: str
    ) -> LLMCallMetadata:
        usage = data.get("usage") or {}
        model = data.get("model")
        meta: LLMCallMetadata = {
            "provider": self.provider,
            "model": model if isinstance(model, str) and model else request_model,
            "finish_reason": str(data.get("stop_reason", "")),
        }
        if usage:
            meta["usage"] = {
                "prompt_tokens": int(usage.get("input_tokens", 0)),
                "completion_tokens": int(usage.get("output_tokens", 0)),
            }
        reasoning = _extract_reasoning_summary(data)
        if reasoning:
            meta["reasoning_summary"] = reasoning[:500]
        return meta


def _split_system(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    """Anthropic takes ``system`` as a top-level field, not a message role."""
    system_parts = [
        str(m.get("content", "")) for m in messages if m.get("role") == "system"
    ]
    chat = [m for m in messages if m.get("role") != "system"]
    return "\n".join(p for p in system_parts if p), chat


def _extract_text(data: dict[str, Any]) -> str:
    blocks = data.get("content") or []
    return "".join(
        str(b.get("text", "")) for b in blocks if b.get("type") == "text"
    )


def _extract_reasoning_summary(data: dict[str, Any]) -> str:
    blocks = data.get("content") or []
    summaries: list[str] = []
    for block in blocks:
        if not isinstance(block, dict) or block.get("type") != "thinking":
            continue
        value = block.get("thinking", "")
        if value:
            summaries.append(str(value))
    return "\n".join(summaries)


def _string_option(value: Any, default: str) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return default


def _positive_int_option(value: Any, default: int) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int | float) and math.isfinite(value) and value > 0:
        return int(value)
    return default


def _non_negative_float_option(value: Any, default: float) -> float:
    if isinstance(value, bool):
        return default
    if isinstance(value, int | float) and math.isfinite(value) and value >= 0:
        return float(value)
    return default
