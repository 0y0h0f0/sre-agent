"""Deterministic fake adapter — migrates FakeLLM under the provider protocol.

The underlying :class:`~packages.agent.fake_llm.FakeLLM` keeps the deterministic
diagnosis/action maps used by both tests and node fallbacks. This adapter adds
protocol-conformant signatures (an optional ``thinking`` flag) and records call
metadata so the factory can return a uniform provider type.
"""

from __future__ import annotations

from typing import Any

from packages.agent.fake_llm import FakeLLM
from packages.agent.llm.base import LLMCallMetadata

_FAKE_MODEL = "fake-diagnosis-model"


class FakeLLMAdapter(FakeLLM):
    """FakeLLM wrapped to satisfy the LLMProvider protocol."""

    provider = "fake"

    def __init__(self) -> None:
        self.last_metadata: LLMCallMetadata = {}

    def invoke(
        self, messages: list[dict[str, Any]], *, thinking: bool = False, **kwargs: Any
    ) -> str:
        result, meta = self.invoke_with_metadata(messages, thinking=thinking, **kwargs)
        self.last_metadata = meta
        return result

    def invoke_with_metadata(
        self, messages: list[dict[str, Any]], *, thinking: bool = False, **kwargs: Any
    ) -> tuple[str, LLMCallMetadata]:
        result = super().invoke(messages)
        meta = self._metadata()
        return result, dict(meta)

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
        result = super().generate_json(prompt, output_schema)
        meta = self._metadata()
        return result, dict(meta)

    def _metadata(self) -> LLMCallMetadata:
        return {
            "provider": self.provider,
            "model": _FAKE_MODEL,
            "usage": {"prompt_tokens": 0, "completion_tokens": 0},
            "finish_reason": "stop",
        }
