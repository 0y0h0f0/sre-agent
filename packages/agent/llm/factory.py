"""Provider factory — selects an LLM adapter from settings (roadmap Phase 1.1).

This replaces the hardcoded ``FakeLLM()`` in the worker. ``llm_provider=fake``
keeps every existing test deterministic; other providers build real adapters
that are only contacted when configured for a live run.
"""

from __future__ import annotations

from packages.agent.llm.anthropic_adapter import AnthropicAdapter
from packages.agent.llm.base import LLMProvider
from packages.agent.llm.fake_adapter import FakeLLMAdapter
from packages.agent.llm.openai_adapter import OpenAICompatibleAdapter
from packages.common.errors import ValidationAppError
from packages.common.settings import Settings

_OPENAI_COMPATIBLE = {"vllm", "openai", "deepseek"}


def build_llm(settings: Settings) -> LLMProvider:
    """Construct the configured LLM provider adapter."""
    provider = settings.llm_provider.strip().lower()
    api_key = settings.llm_api_key.get_secret_value() if settings.llm_api_key else None

    if provider == "fake":
        return FakeLLMAdapter()

    if provider in _OPENAI_COMPATIBLE:
        return OpenAICompatibleAdapter(
            base_url=settings.llm_base_url,
            model=settings.llm_model,
            api_key=api_key,
            provider_name=provider,
            timeout_seconds=settings.llm_timeout_seconds,
            max_tokens=settings.llm_max_tokens,
            temperature=settings.llm_temperature,
            reasoning_enabled=settings.llm_reasoning_enabled,
            reasoning_effort=settings.llm_reasoning_effort,
        )

    if provider == "anthropic":
        return AnthropicAdapter(
            model=settings.llm_model,
            api_key=api_key,
            timeout_seconds=settings.llm_timeout_seconds,
            max_tokens=settings.llm_max_tokens,
            temperature=settings.llm_temperature,
            reasoning_enabled=settings.llm_reasoning_enabled,
            reasoning_effort=settings.llm_reasoning_effort,
        )

    raise ValidationAppError(
        f"unknown llm_provider '{settings.llm_provider}'",
        details={"supported": ["fake", *sorted(_OPENAI_COMPATIBLE), "anthropic"]},
    )
