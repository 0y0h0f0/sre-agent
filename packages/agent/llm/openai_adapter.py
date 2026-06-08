"""OpenAI-compatible adapter — covers local vLLM and OpenAI/DeepSeek APIs.

All three speak the OpenAI ``/chat/completions`` schema, so a single adapter
serves roadmap Phase 1 scenarios A (local vLLM) and B (cloud OpenAI/DeepSeek).
Network access is lazy; tests inject an ``httpx.Client`` built on a mock
transport so no real endpoint is contacted.
"""

from __future__ import annotations

from typing import Any

import httpx

from packages.agent.llm.base import LLMCallMetadata, extract_json, parse_into_schema

_JSON_SYSTEM_PROMPT = (
    "You are an SRE diagnosis assistant. Respond with a single valid JSON object "
    "or array only — no prose, no Markdown fences."
    "最后用中文回答问题"
)


class OpenAICompatibleAdapter:
    """Adapter for any OpenAI-compatible chat completions endpoint."""

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str | None = None,
        provider_name: str = "vllm",
        timeout_seconds: float = 30.0,
        max_tokens: int = 512,
        temperature: float = 0.1,
        reasoning_enabled: bool = False,
        reasoning_effort: str = "medium",
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.provider = provider_name
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
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
        }
        if self.provider == "deepseek":
            # DeepSeek thinking defaults to enabled, so standard nodes must opt out
            # explicitly. The OpenAI SDK calls this via extra_body; raw HTTP sends
            # the provider extension as a normal request field.
            payload["thinking"] = {"type": "enabled" if thinking else "disabled"}
            if thinking:
                payload["reasoning_effort"] = _deepseek_effort(self.reasoning_effort)
        elif thinking:
            # Other OpenAI-compatible reasoning endpoints use reasoning_effort when
            # the node-level scheduler requests deep reasoning.
            payload["reasoning_effort"] = self.reasoning_effort
        started = __import__("time").perf_counter()
        data = self._post("/chat/completions", payload)
        choice = (data.get("choices") or [{}])[0]
        content = choice.get("message", {}).get("content", "")
        if content is None:
            content = ""
        self._record(data, choice)
        _emit_llm_metrics(self, data, choice, started)
        return str(content)

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
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        if self._client is not None:
            response = self._client.post(
                path, json=payload, headers=headers, timeout=self.timeout_seconds
            )
        else:
            with httpx.Client(base_url=self.base_url, timeout=self.timeout_seconds) as client:
                response = client.post(path, json=payload, headers=headers)
        try:
            response.raise_for_status()
        except Exception:
            from packages.common import metrics as agent_metrics

            agent_metrics.AgentMetricsCollector.record_llm_error(
                model=self.model,
                provider=self.provider,
                error_type=type(response).__name__[:40],
            )
            raise
        result: dict[str, Any] = response.json()
        return result

    def _record(self, data: dict[str, Any], choice: dict[str, Any]) -> None:
        usage = data.get("usage") or {}
        meta: LLMCallMetadata = {
            "provider": self.provider,
            "model": str(data.get("model", self.model)),
            "finish_reason": str(choice.get("finish_reason", "")),
        }
        if usage:
            meta["usage"] = {k: int(v) for k, v in usage.items() if isinstance(v, int | float)}
        reasoning = choice.get("message", {}).get("reasoning_content")
        if reasoning:
            meta["reasoning_summary"] = str(reasoning)[:500]
        self.last_metadata = meta


def _deepseek_effort(value: str) -> str:
    effort = value.strip().lower()
    if effort in {"max", "xhigh"}:
        return "max"
    return "high"


def _emit_llm_metrics(
    adapter: OpenAICompatibleAdapter,
    data: dict[str, Any],
    choice: dict[str, Any],
    started: float,
) -> None:
    """Record Prometheus metrics for a completed LLM call."""
    from time import perf_counter

    from packages.common import metrics as agent_metrics
    from packages.common.metrics import _sanitize_label

    usage = data.get("usage") or {}
    prompt_tokens = int(usage.get("prompt_tokens", 0))
    completion_tokens = int(usage.get("completion_tokens", 0))
    cache_hit = bool(choice.get("finish_reason") == "cache_hit")
    agent_metrics.llm_prompt_tokens_total.labels(
        model=_sanitize_label(adapter.model),
        provider=_sanitize_label(adapter.provider),
    ).inc(prompt_tokens)
    agent_metrics.llm_completion_tokens_total.labels(
        model=_sanitize_label(adapter.model),
        provider=_sanitize_label(adapter.provider),
    ).inc(completion_tokens)
    agent_metrics.llm_call_duration_seconds.labels(
        model=_sanitize_label(adapter.model),
        provider=_sanitize_label(adapter.provider),
    ).observe(perf_counter() - started)
    if cache_hit:
        agent_metrics.llm_cache_hit_total.labels(
            provider=_sanitize_label(adapter.provider)
        ).inc()
    else:
        agent_metrics.llm_cache_miss_total.labels(
            provider=_sanitize_label(adapter.provider)
        ).inc()
