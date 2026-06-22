"""Redacting LLM adapter for external cloud providers.

This wrapper is the last boundary before data leaves the process for a cloud
LLM. It redacts prompt/message strings and records safe redaction metadata while
delegating provider-specific transport, parsing, and token accounting.
"""

from __future__ import annotations

from typing import Any, cast

from packages.agent.llm.base import LLMCallMetadata, LLMProvider
from packages.common.redaction import redact_text


class RedactingLLMAdapter:
    """Apply deterministic text redaction before delegating to an LLM provider."""

    def __init__(self, delegate: LLMProvider) -> None:
        self.delegate = delegate
        self.last_metadata: LLMCallMetadata = {}

    @property
    def provider(self) -> str:
        return str(getattr(self.delegate, "provider", "unknown"))

    @property
    def model_name(self) -> str:
        return str(
            getattr(self.delegate, "model_name", getattr(self.delegate, "model", "unknown"))
        )

    def invoke(
        self, messages: list[dict[str, Any]], *, thinking: bool = False, **kwargs: Any
    ) -> str:
        result, meta = self.invoke_with_metadata(messages, thinking=thinking, **kwargs)
        self.last_metadata = meta
        return result

    def invoke_with_metadata(
        self, messages: list[dict[str, Any]], *, thinking: bool = False, **kwargs: Any
    ) -> tuple[str, LLMCallMetadata]:
        redacted_messages, redaction = _redact_value(messages)
        try:
            if hasattr(self.delegate, "invoke_with_metadata"):
                result, meta = self.delegate.invoke_with_metadata(
                    redacted_messages, thinking=thinking, **kwargs
                )
            else:
                result = self.delegate.invoke(redacted_messages, thinking=thinking, **kwargs)
                meta = dict(getattr(self.delegate, "last_metadata", None) or {})
            return result, self._metadata_with_redaction(redaction, meta)
        except Exception:
            raise

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
        redacted_prompt, redaction = _redact_value(prompt)
        try:
            if hasattr(self.delegate, "generate_json_with_metadata"):
                result, meta = self.delegate.generate_json_with_metadata(
                    redacted_prompt, output_schema, thinking=thinking, **kwargs
                )
            else:
                result = self.delegate.generate_json(
                    redacted_prompt, output_schema, thinking=thinking, **kwargs
                )
                meta = dict(getattr(self.delegate, "last_metadata", None) or {})
            return result, self._metadata_with_redaction(redaction, meta)
        except Exception:
            raise

    def _metadata_with_redaction(
        self, redaction: _RedactionSummary, metadata: dict[str, Any]
    ) -> LLMCallMetadata:
        meta = dict(metadata)
        meta["redaction_applied"] = redaction.count > 0
        meta["redaction_count"] = redaction.count
        meta["redaction_types"] = sorted(set(redaction.types))
        return cast(LLMCallMetadata, meta)


class _RedactionSummary:
    def __init__(self) -> None:
        self.count = 0
        self.types: list[str] = []

    def add(self, count: int, types: list[str]) -> None:
        self.count += count
        self.types.extend(types)

    def merge(self, other: _RedactionSummary) -> None:
        self.count += other.count
        self.types.extend(other.types)


def _redact_value(value: Any) -> tuple[Any, _RedactionSummary]:
    summary = _RedactionSummary()
    if isinstance(value, str):
        redacted = redact_text(value)
        summary.add(redacted.redaction_count, redacted.redaction_types)
        return redacted.redacted_text, summary
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            cleaned, child = _redact_value(item)
            result[key] = cleaned
            summary.merge(child)
        return result, summary
    if isinstance(value, list):
        result: list[Any] = []
        for item in value:
            cleaned, child = _redact_value(item)
            result.append(cleaned)
            summary.merge(child)
        return result, summary
    return value, summary
