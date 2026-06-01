"""Shared interfaces and result models for observability tools."""

from __future__ import annotations

from collections.abc import Mapping
from time import perf_counter
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

ToolStatus = Literal["succeeded", "failed", "degraded", "timeout"]


class ToolResult(BaseModel):
    status: ToolStatus
    data: dict[str, Any] = Field(default_factory=dict)
    summary: str
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    cache_key: str | None = None
    cache_hit: bool = False
    duration_ms: int
    error_message: str | None = None


class BaseTool(Protocol):
    name: str
    timeout_seconds: float

    def run(self, query: BaseModel) -> ToolResult:
        """Execute the tool query and return a structured result."""


def start_timer() -> float:
    return perf_counter()


def elapsed_ms(started_at: float) -> int:
    return max(0, int((perf_counter() - started_at) * 1000))


def compact_summary(fields: Mapping[str, Any]) -> str:
    parts = [f"{key}={value}" for key, value in fields.items() if value not in (None, "", [])]
    return ", ".join(parts)
