"""LLM provider abstraction layer (roadmap Phase 1.1).

See ``doc/11-roadmap/phase-1-intelligent-diagnosis.md`` for the design.
"""

from __future__ import annotations

from packages.agent.llm.anthropic_adapter import AnthropicAdapter
from packages.agent.llm.base import LLMCallMetadata, LLMProvider
from packages.agent.llm.factory import build_llm
from packages.agent.llm.fake_adapter import FakeLLMAdapter
from packages.agent.llm.openai_adapter import OpenAICompatibleAdapter
from packages.agent.llm.redacting_adapter import RedactingLLMAdapter

__all__ = [
    "AnthropicAdapter",
    "FakeLLMAdapter",
    "LLMCallMetadata",
    "LLMProvider",
    "OpenAICompatibleAdapter",
    "RedactingLLMAdapter",
    "build_llm",
]
