"""Trace lookup tool backed by demo fixtures for MVP."""

from __future__ import annotations

import json
from datetime import datetime
from math import ceil
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from packages.common.time import ensure_utc
from packages.tools.base import ToolResult, ToolStatus, compact_summary, elapsed_ms, start_timer
from packages.tools.cache import RequestLocalToolCache, build_cache_key


class TraceQuery(BaseModel):
    service: str = Field(min_length=1)
    start: datetime
    end: datetime
    min_duration_ms: int = Field(default=500, ge=0)

    @field_validator("service")
    @classmethod
    def _strip_service(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def _validate_window(self) -> TraceQuery:
        self.start = ensure_utc(self.start)
        self.end = ensure_utc(self.end)
        if self.end <= self.start:
            msg = "end must be after start"
            raise ValueError(msg)
        return self


class TraceTool:
    name = "traces"

    def __init__(
        self,
        *,
        fixture_path: str | Path = "demo/faults/traces.json",
        timeout_seconds: float = 2.0,
        cache: RequestLocalToolCache | None = None,
    ) -> None:
        self.fixture_path = Path(fixture_path)
        self.timeout_seconds = timeout_seconds
        self.cache = cache

    def run(self, query: BaseModel) -> ToolResult:
        trace_query = TraceQuery.model_validate(query)
        started_at = start_timer()
        cache_key = build_cache_key(
            tool_name=self.name,
            service=trace_query.service,
            query=trace_query,
            start=trace_query.start,
            end=trace_query.end,
            bucket_seconds=300,
        )
        cached = self.cache.get(cache_key) if self.cache else None
        if cached is not None:
            return cached.model_copy(update={"duration_ms": elapsed_ms(started_at)})

        try:
            spans = self._load_spans()
            matching = [_normalize_span(span) for span in spans]
            matching = [
                span
                for span in matching
                if span["service"] == trace_query.service
                and trace_query.start <= span["start"] <= trace_query.end
            ]
            slow_spans = [
                span for span in matching if span["duration_ms"] >= trace_query.min_duration_ms
            ]
            error_spans = [span for span in matching if span.get("status") == "error"]
            downstream = sorted(
                {
                    span["downstream_service"]
                    for span in matching
                    if isinstance(span.get("downstream_service"), str)
                }
            )
            durations = [span["duration_ms"] for span in matching]
            p95 = _p95(durations) if durations else None
            data = {
                "span_count": len(matching),
                "slow_spans": [_public_span(span) for span in slow_spans[:10]],
                "error_spans": [_public_span(span) for span in error_spans[:10]],
                "downstream_services": downstream,
                "duration_p95_ms": p95,
            }
            status: ToolStatus = "succeeded" if matching else "degraded"
            result = ToolResult(
                status=status,
                data=data,
                summary=(
                    compact_summary(
                        {
                            "service": trace_query.service,
                            "spans": len(matching),
                            "slow": len(slow_spans),
                            "errors": len(error_spans),
                            "p95_ms": p95,
                        }
                    )
                    if matching
                    else f"no trace spans for {trace_query.service}"
                ),
                evidence=[
                    {
                        "type": "trace",
                        "source": "demo-trace-fixture",
                        "title": f"trace spans for {trace_query.service}",
                        "payload": data,
                    }
                ]
                if matching
                else [],
                cache_key=cache_key,
                duration_ms=elapsed_ms(started_at),
                error_message=None if matching else "empty trace fixture result",
            )
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            result = ToolResult(
                status="degraded",
                data={},
                summary=f"trace fixture unavailable for {trace_query.service}",
                cache_key=cache_key,
                duration_ms=elapsed_ms(started_at),
                error_message=str(exc),
            )

        if self.cache and result.status in {"succeeded", "degraded"}:
            self.cache.set(cache_key, result)
        return result

    def _load_spans(self) -> list[dict[str, Any]]:
        payload = json.loads(self.fixture_path.read_text(encoding="utf-8"))
        spans = payload.get("spans", [])
        if not isinstance(spans, list):
            msg = "spans must be a list"
            raise ValueError(msg)
        return [span for span in spans if isinstance(span, dict)]


def _normalize_span(span: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(span)
    normalized["start"] = ensure_utc(_parse_datetime(str(span["start"])))
    normalized["duration_ms"] = int(span["duration_ms"])
    return normalized


def _parse_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _p95(values: list[int]) -> int:
    ordered = sorted(values)
    index = max(0, ceil(len(ordered) * 0.95) - 1)
    return ordered[index]


def _public_span(span: dict[str, Any]) -> dict[str, Any]:
    return {
        "trace_id": span.get("trace_id"),
        "span_id": span.get("span_id"),
        "name": span.get("name"),
        "duration_ms": span.get("duration_ms"),
        "status": span.get("status"),
        "downstream_service": span.get("downstream_service"),
    }
