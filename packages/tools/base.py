"""Shared interfaces and result models for observability tools.

Agent nodes consume tools through this small contract instead of reaching into
Prometheus, Loki, Kubernetes, PostgreSQL, or RAG internals. Keeping the result
shape uniform is what lets the graph persist evidence, record tool calls, and
degrade gracefully when one backend is unavailable.
"""

from __future__ import annotations

from collections.abc import Mapping
from time import perf_counter
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

ToolStatus = Literal["succeeded", "failed", "degraded", "timeout"]


class ToolResult(BaseModel):
    """Common return envelope for every diagnostic/read tool.

    ``status`` describes tool health, while ``evidence`` contains compact,
    persistence-ready facts that may later receive ``evidence_id`` values. Large
    raw payloads and secrets should not be placed here.
    """

    status: ToolStatus
    data: dict[str, Any] = Field(default_factory=dict)
    summary: str
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    cache_key: str | None = None
    cache_hit: bool = False
    duration_ms: int
    error_message: str | None = None


class BaseTool(Protocol):
    """Synchronous diagnostic tool protocol used by agent nodes."""

    name: str
    timeout_seconds: float

    def run(self, query: BaseModel) -> ToolResult:
        """Execute the tool query and return a structured result."""


def start_timer() -> float:
    """Start a monotonic timer for tool duration accounting."""
    return perf_counter()


def elapsed_ms(started_at: float) -> int:
    """Return elapsed wall time in milliseconds, clamped at zero."""
    return max(0, int((perf_counter() - started_at) * 1000))


def compact_summary(fields: Mapping[str, Any]) -> str:
    """Build a concise audit summary from non-empty key/value pairs."""
    parts = [f"{key}={value}" for key, value in fields.items() if value not in (None, "", [])]
    return ", ".join(parts)
