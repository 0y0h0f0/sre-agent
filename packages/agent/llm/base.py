"""LLM provider protocol and shared parsing helpers (roadmap Phase 1.1).

The protocol is intentionally compatible with the existing synchronous LangGraph
nodes, which call ``generate_json(prompt, schema)`` and ``invoke(messages)``.
Adapters add an optional ``thinking`` flag so reasoning-depth layering (Phase 1.2)
can be wired in later without changing the node call sites.
"""

from __future__ import annotations

import json
from typing import Any, Literal, Protocol, TypedDict, get_args, get_origin, runtime_checkable

from pydantic import BaseModel


class LLMCallMetadata(TypedDict, total=False):
    """Auditable metadata about a single LLM call.

    ``reasoning_summary`` holds a short, auditable rationale when a provider
    returns reasoning content. The raw chain-of-thought is never persisted by
    default (see roadmap Phase 1.2).
    """

    model: str
    provider: str
    usage: dict[str, int]
    provider_cache_status: Literal["hit", "miss", "unknown"]
    duration_ms: int
    service_tier: str
    reasoning_summary: str
    finish_reason: str
    redaction_applied: bool
    redaction_count: int
    redaction_types: list[str]


@runtime_checkable
class LLMProvider(Protocol):
    """Synchronous LLM provider compatible with the current agent nodes."""

    def invoke(
        self, messages: list[dict[str, Any]], *, thinking: bool = False, **kwargs: Any
    ) -> str:
        """Return the raw text completion for a chat-style message list."""

    def generate_json(
        self, prompt: str, output_schema: Any, *, thinking: bool = False, **kwargs: Any
    ) -> Any:
        """Return a parsed object (BaseModel or list of BaseModel) for ``prompt``."""

    def invoke_with_metadata(
        self, messages: list[dict[str, Any]], *, thinking: bool = False, **kwargs: Any
    ) -> tuple[str, LLMCallMetadata]:
        """Return text plus metadata without requiring shared ``last_metadata`` reads."""

    def generate_json_with_metadata(
        self, prompt: str, output_schema: Any, *, thinking: bool = False, **kwargs: Any
    ) -> tuple[Any, LLMCallMetadata]:
        """Return parsed output plus metadata without shared ``last_metadata`` reads."""


def extract_json(text: str) -> Any:
    """Parse JSON from a model response, tolerating Markdown code fences.

    Raises ``ValueError`` (via ``json.JSONDecodeError``) when no JSON is found so
    callers can fall back to deterministic behaviour.
    """
    cleaned = text.strip()
    if cleaned.startswith("```"):
        # Drop the opening fence (``` or ```json) and the trailing fence.
        cleaned = cleaned.split("\n", 1)[-1] if "\n" in cleaned else cleaned
        if cleaned.endswith("```"):
            cleaned = cleaned[: -len("```")]
        cleaned = cleaned.strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        # Last resort: extract the first balanced {...} or [...] span.
        span = _first_json_span(cleaned)
        if span is None:
            raise
        return json.loads(span)


def parse_into_schema(data: Any, output_schema: Any) -> Any:
    """Coerce parsed JSON ``data`` into ``output_schema``.

    Supports a single ``BaseModel`` subclass or ``list[SomeModel]``.
    """
    origin = get_origin(output_schema)
    if origin is list:
        (item_type,) = get_args(output_schema) or (dict,)
        items = data if isinstance(data, list) else [data]
        if isinstance(item_type, type) and issubclass(item_type, BaseModel):
            return [item_type(**item) for item in items]
        return items
    if isinstance(output_schema, type) and issubclass(output_schema, BaseModel):
        return output_schema(**data)
    return data


def _first_json_span(text: str) -> str | None:
    """Extract the JSON object/array span from *text*, preferring the last ``{{``.

    Real LLMs may emit reasoning text before the JSON output.  We find the
    last ``}}``, walk backwards to the nearest ``{{`` before it, and validate
    the span is valid JSON.  Falls back to first-``{{``-to-last-``}}``.
    """
    for open_ch, close_ch in (("{", "}"), ("[", "]")):
        end = text.rfind(close_ch)
        if end < 0:
            continue
        # Prefer last { before last }
        start = text.rfind(open_ch, 0, end)
        if 0 <= start < end:
            candidate = text[start : end + 1]
            try:
                json.loads(candidate)
                return candidate
            except json.JSONDecodeError:
                pass
        # Fallback: first { → last }
        start = text.find(open_ch)
        if 0 <= start < end:
            return text[start : end + 1]
    return None
