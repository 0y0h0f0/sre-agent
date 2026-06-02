"""Anthropic Claude adapter (roadmap Phase 1 scenario B).

Uses the Messages API over httpx. Adaptive thinking is requested when reasoning
is enabled; only an auditable rationale/text is returned to callers, never the
raw thinking blocks.
"""

from __future__ import annotations

from typing import Any

import httpx

from packages.agent.llm.base import LLMCallMetadata, extract_json, parse_into_schema
from packages.common.errors import ValidationAppError

_DEFAULT_BASE_URL = "https://api.anthropic.com"
_ANTHROPIC_VERSION = "2023-06-01"
_JSON_SYSTEM_PROMPT = (
    "You are an SRE diagnosis assistant. Respond with a single valid JSON object "
    "or array only — no prose, no Markdown fences."
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
        system, chat = _split_system(messages)
        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "messages": chat,
        }
        if system:
            payload["system"] = system
        if thinking or self.reasoning_enabled:
            payload["thinking"] = {"type": "adaptive"}
            payload["output_config"] = {"effort": self.reasoning_effort}
        data = self._post("/v1/messages", payload)
        text = _extract_text(data)
        self._record(data)
        return text

    def generate_json(
        self, prompt: str, output_schema: Any, *, thinking: bool = False, **kwargs: Any
    ) -> Any:
        messages = [
            {"role": "system", "content": _JSON_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ]
        raw = self.invoke(messages, thinking=thinking)
        return parse_into_schema(extract_json(raw), output_schema)

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

    def _record(self, data: dict[str, Any]) -> None:
        usage = data.get("usage") or {}
        meta: LLMCallMetadata = {
            "provider": self.provider,
            "model": str(data.get("model", self.model)),
            "finish_reason": str(data.get("stop_reason", "")),
        }
        if usage:
            meta["usage"] = {
                "prompt_tokens": int(usage.get("input_tokens", 0)),
                "completion_tokens": int(usage.get("output_tokens", 0)),
            }
        self.last_metadata = meta


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
