"""Disabled LLM adapter — deterministic fallback with no network calls.

Used when APP_ENV=production and LLM_PROVIDER defaults to 'disabled'.
All responses come from deterministic rules, not external API calls.
"""

from __future__ import annotations

from typing import Any

from packages.agent.llm.base import LLMCallMetadata, LLMProvider


class DisabledLLMAdapter(LLMProvider):
    """Deterministic adapter that never makes network calls.

    Delegates to FakeLLM's deterministic logic but marks all metadata
    with provider=disabled, network_call=false, fallback=true.
    """

    provider = "disabled"

    def __init__(self) -> None:
        from packages.agent.fake_llm import FakeLLM

        self._fake = FakeLLM()
        self.last_metadata: LLMCallMetadata = {}

    def invoke(
        self,
        messages: list[dict[str, Any]],
        *,
        thinking: bool = False,
        **kwargs: Any,
    ) -> str:
        result = self._fake.invoke(messages, thinking=thinking, **kwargs)
        self.last_metadata = LLMCallMetadata(
            provider="disabled",
            model="deterministic-fallback",
            usage={"prompt_tokens": 0, "completion_tokens": 0},
            finish_reason="stop",
        )
        return result

    def generate_json(
        self,
        prompt: str,
        output_schema: Any,
        *,
        thinking: bool = False,
        **kwargs: Any,
    ) -> Any:
        result = self._fake.generate_json(
            prompt, output_schema, thinking=thinking, **kwargs
        )
        self.last_metadata = LLMCallMetadata(
            provider="disabled",
            model="deterministic-fallback",
            usage={"prompt_tokens": 0, "completion_tokens": 0},
            finish_reason="stop",
        )
        return result

    def get_last_metadata(self) -> LLMCallMetadata:
        return self.last_metadata
