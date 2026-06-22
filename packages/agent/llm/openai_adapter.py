"""OpenAI-compatible adapter — covers local vLLM and OpenAI/DeepSeek APIs.

All three speak the OpenAI ``/chat/completions`` schema, so a single adapter
serves roadmap Phase 1 scenarios A (local vLLM) and B (cloud OpenAI/DeepSeek).
Network access is lazy; tests inject an ``httpx.Client`` built on a mock
transport so no real endpoint is contacted.
"""

from __future__ import annotations

import math
from time import perf_counter
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
        content, meta = self.invoke_with_metadata(
            messages, thinking=thinking, **kwargs
        )
        self.last_metadata = meta
        return content

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
        payload: dict[str, Any] = {
            "model": request_model,
            "messages": messages,
            "max_tokens": request_max_tokens,
            "temperature": request_temperature,
        }
        if self.provider == "deepseek":
            # DeepSeek thinking defaults to enabled, so standard nodes must opt out
            # explicitly. The OpenAI SDK calls this via extra_body; raw HTTP sends
            # the provider extension as a normal request field.
            payload["thinking"] = {"type": "enabled" if thinking else "disabled"}
            if thinking:
                payload["reasoning_effort"] = _deepseek_effort(request_reasoning_effort)
        elif thinking:
            # Other OpenAI-compatible reasoning endpoints use reasoning_effort when
            # the node-level scheduler requests deep reasoning.
            payload["reasoning_effort"] = request_reasoning_effort
        started = perf_counter()
        data = self._post("/chat/completions", payload, model_for_metrics=request_model)
        duration_seconds = max(0.0, perf_counter() - started)
        choice = (data.get("choices") or [{}])[0]
        content = choice.get("message", {}).get("content", "")
        if content is None:
            content = ""
        meta = self._metadata_from_response(
            data,
            choice,
            duration_ms=int(duration_seconds * 1000),
            request_model=request_model,
        )
        _emit_llm_metrics(self, data, duration_seconds, model=request_model)
        return str(content), dict(meta)

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

    def _post(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        model_for_metrics: str | None = None,
    ) -> dict[str, Any]:
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
                model=model_for_metrics or self.model,
                provider=self.provider,
                error_type=type(response).__name__[:40],
            )
            raise
        result: dict[str, Any] = response.json()
        return result

    def _metadata_from_response(
        self,
        data: dict[str, Any],
        choice: dict[str, Any],
        *,
        duration_ms: int,
        request_model: str,
    ) -> LLMCallMetadata:
        usage = data.get("usage") or {}
        safe_usage, provider_cache_status = _safe_usage_metadata(usage)
        model = data.get("model")
        finish_reason = choice.get("finish_reason")
        meta: LLMCallMetadata = {
            "provider": self.provider,
            "model": model if isinstance(model, str) and model else request_model,
            "finish_reason": finish_reason if isinstance(finish_reason, str) else "",
            "provider_cache_status": provider_cache_status,
            "duration_ms": duration_ms,
        }
        if safe_usage:
            meta["usage"] = safe_usage
        service_tier = data.get("service_tier")
        if isinstance(service_tier, str) and service_tier:
            meta["service_tier"] = service_tier
        return meta


def _deepseek_effort(value: str) -> str:
    effort = value.strip().lower()
    if effort in {"max", "xhigh"}:
        return "max"
    return "high"


def _emit_llm_metrics(
    adapter: OpenAICompatibleAdapter,
    data: dict[str, Any],
    duration_seconds: float,
    *,
    model: str,
) -> None:
    """Record Prometheus metrics for a completed LLM call."""
    from packages.common import metrics as agent_metrics

    usage, provider_cache_status = _safe_usage_metadata(data.get("usage") or {})
    agent_metrics.AgentMetricsCollector.record_llm_usage(
        model=model,
        provider=adapter.provider,
        prompt_tokens=usage.get("prompt_tokens", 0),
        completion_tokens=usage.get("completion_tokens", 0),
        cached_prompt_tokens=usage.get("cached_prompt_tokens", 0),
        duration_seconds=duration_seconds,
        provider_cache_status=provider_cache_status,
    )


def _safe_usage_metadata(usage: Any) -> tuple[dict[str, int], str]:
    """Return allowlisted token usage and provider cache status.

    Provider prompt cache status is tri-state. It is only hit/miss when the
    provider exposes explicit cache-token details; otherwise it remains unknown.
    """
    if not isinstance(usage, dict):
        return {}, "unknown"

    safe: dict[str, int] = {}
    for source_key, output_key in (
        ("prompt_tokens", "prompt_tokens"),
        ("completion_tokens", "completion_tokens"),
        ("total_tokens", "total_tokens"),
    ):
        value = _safe_int(usage.get(source_key))
        if value is not None:
            safe[output_key] = value

    cache_detail_present = False
    cached_prompt_tokens = None
    prompt_details = usage.get("prompt_tokens_details")
    if isinstance(prompt_details, dict) and "cached_tokens" in prompt_details:
        cached_prompt_tokens = _safe_int(prompt_details.get("cached_tokens"))
        if cached_prompt_tokens is not None:
            cache_detail_present = True
            safe["cached_prompt_tokens"] = cached_prompt_tokens

    reasoning_tokens = _nested_safe_int(
        usage,
        ("completion_tokens_details", "reasoning_tokens"),
        ("output_tokens_details", "reasoning_tokens"),
        ("reasoning_tokens",),
    )
    if reasoning_tokens is not None:
        safe["reasoning_tokens"] = reasoning_tokens

    if not cache_detail_present:
        return safe, "unknown"
    if cached_prompt_tokens and cached_prompt_tokens > 0:
        return safe, "hit"
    return safe, "miss"


def _nested_safe_int(data: dict[str, Any], *paths: tuple[str, ...]) -> int | None:
    for path in paths:
        current: Any = data
        for key in path:
            if not isinstance(current, dict) or key not in current:
                current = None
                break
            current = current[key]
        value = _safe_int(current)
        if value is not None:
            return value
    return None


def _safe_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, int | float):
        if value < 0:
            return None
        return int(value)
    return None


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
