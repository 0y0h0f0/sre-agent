"""Provider factory — selects an LLM adapter from settings (roadmap Phase 1.1).

This replaces the hardcoded ``FakeLLM()`` in the worker. ``llm_provider=fake``
keeps every existing test deterministic; other providers build real adapters
that are only contacted when configured for a live run.
"""

from __future__ import annotations

from packages.agent.llm.anthropic_adapter import AnthropicAdapter
from packages.agent.llm.base import LLMProvider
from packages.agent.llm.disabled_adapter import DisabledLLMAdapter
from packages.agent.llm.fake_adapter import FakeLLMAdapter
from packages.agent.llm.openai_adapter import OpenAICompatibleAdapter
from packages.agent.llm.profiles import resolve_llm_profile
from packages.agent.llm.redacting_adapter import RedactingLLMAdapter
from packages.common.errors import ValidationAppError
from packages.common.settings import Settings

_OPENAI_COMPATIBLE = {"vllm", "openai", "deepseek"}
_EXTERNAL_CLOUD_PROVIDERS = {"openai", "deepseek", "anthropic"}


def build_llm(settings: Settings, profile: str | None = None) -> LLMProvider:
    """Construct the configured LLM provider adapter."""
    provider = settings.llm_provider.strip().lower()
    api_key = settings.llm_api_key.get_secret_value() if settings.llm_api_key else None

    if provider == "fake":
        return FakeLLMAdapter()

    if provider == "disabled":
        return DisabledLLMAdapter()

    if provider in _EXTERNAL_CLOUD_PROVIDERS and not settings.llm_external_provider_allowed:
        raise ValidationAppError(
            "external LLM provider requires LLM_EXTERNAL_PROVIDER_ALLOWED=true",
            details={
                "provider": provider,
                "required_setting": "LLM_EXTERNAL_PROVIDER_ALLOWED",
            },
        )

    llm_profile = resolve_llm_profile(settings, profile)

    if provider in _OPENAI_COMPATIBLE:
        adapter = OpenAICompatibleAdapter(
            base_url=settings.llm_base_url,
            model=llm_profile.model,
            api_key=api_key,
            provider_name=provider,
            timeout_seconds=settings.llm_timeout_seconds,
            max_tokens=llm_profile.max_tokens,
            temperature=llm_profile.temperature,
            reasoning_enabled=settings.llm_reasoning_enabled,
            reasoning_effort=llm_profile.reasoning_effort,
        )
        return _wrap_external_provider(provider, adapter)

    if provider == "anthropic":
        adapter = AnthropicAdapter(
            model=llm_profile.model,
            api_key=api_key,
            timeout_seconds=settings.llm_timeout_seconds,
            max_tokens=llm_profile.max_tokens,
            temperature=llm_profile.temperature,
            reasoning_enabled=settings.llm_reasoning_enabled,
            reasoning_effort=llm_profile.reasoning_effort,
        )
        return _wrap_external_provider(provider, adapter)

    raise ValidationAppError(
        f"unknown llm_provider '{settings.llm_provider}'",
        details={"supported": ["fake", *sorted(_OPENAI_COMPATIBLE), "anthropic"]},
    )


def _wrap_external_provider(provider: str, adapter: LLMProvider) -> LLMProvider:
    if provider in _EXTERNAL_CLOUD_PROVIDERS:
        return RedactingLLMAdapter(adapter)
    return adapter
