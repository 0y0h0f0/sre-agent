"""Trace lookup tool backed by a pluggable trace backend (Phase 2.1).

The fixture backend keeps MVP behaviour; Jaeger/Tempo backends query a real
trace store. Analysis (slow/error span extraction, p95, downstream services)
lives here so every backend shares one path.
"""

from __future__ import annotations

import json
from datetime import datetime
from math import ceil
from typing import Any

import httpx
from pydantic import BaseModel, Field, field_validator, model_validator

from packages.common.redaction import redact_text
from packages.common.time import ensure_utc
from packages.tools.base import ToolResult, ToolStatus, compact_summary, elapsed_ms, start_timer
from packages.tools.cache import RequestLocalToolCache, build_cache_key
from packages.tools.trace_backends import FixtureTraceBackend, TraceBackend


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
        backend: TraceBackend | None = None,
        fixture_path: str | None = None,
        timeout_seconds: float = 2.0,
        cache: RequestLocalToolCache | None = None,
    ) -> None:
        if backend is None:
            backend = FixtureTraceBackend(fixture_path=fixture_path or "demo/faults/traces.json")
        self.backend = backend
        self.timeout_seconds = timeout_seconds
        self.cache = cache

    def run(self, query: BaseModel) -> ToolResult:
        trace_query = TraceQuery.model_validate(query)
        public_service = _redact_query_text(trace_query.service)
        started_at = start_timer()
        cache_key = build_cache_key(
            tool_name=self.name,
            service=public_service,
            query=trace_query,
            start=trace_query.start,
            end=trace_query.end,
            bucket_seconds=300,
            datasource=self.backend.name,
        )
        cached = self.cache.get(cache_key) if self.cache else None
        if cached is not None:
            return cached.model_copy(update={"duration_ms": elapsed_ms(started_at)})

        try:
            spans = self.backend.fetch_spans(public_service, trace_query.start, trace_query.end)
            matching = [_normalize_span(span) for span in spans]
            matching = [
                span
                for span in matching
                if span["service"] == public_service
                and trace_query.start <= span["start"] <= trace_query.end
            ]
            slow_spans = [
                span for span in matching if span["duration_ms"] >= trace_query.min_duration_ms
            ]
            error_spans = [span for span in matching if span.get("status") == "error"]
            downstream = sorted(
                {
                    _redact_public_text(span["downstream_service"])
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
                            "service": public_service,
                            "spans": len(matching),
                            "slow": len(slow_spans),
                            "errors": len(error_spans),
                            "p95_ms": p95,
                        }
                    )
                    if matching
                    else f"no trace spans for {public_service}"
                ),
                evidence=[
                    {
                        "type": "trace",
                        "source": self.backend.name,
                        "title": f"trace spans for {public_service}",
                        "payload": data,
                    }
                ]
                if matching
                else [],
                cache_key=cache_key,
                duration_ms=elapsed_ms(started_at),
                error_message=None if matching else "empty trace result",
            )
        except httpx.TimeoutException as exc:
            result = ToolResult(
                status="timeout",
                data={},
                summary=f"trace backend timed out for {public_service}",
                cache_key=cache_key,
                duration_ms=elapsed_ms(started_at),
                error_message=_redact_exception(exc),
            )
        except (
            httpx.HTTPError,
            OSError,
            json.JSONDecodeError,
            KeyError,
            TypeError,
            ValueError,
        ) as exc:
            result = ToolResult(
                status="degraded",
                data={},
                summary=f"trace backend unavailable for {public_service}",
                cache_key=cache_key,
                duration_ms=elapsed_ms(started_at),
                error_message=_redact_exception(exc),
            )

        if self.cache and result.status in {"succeeded", "degraded"}:
            self.cache.set(cache_key, result)
        return result


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
        "trace_id": _redact_trace_identifier(span.get("trace_id")),
        "span_id": _redact_trace_identifier(span.get("span_id")),
        "name": _redact_public_text(span.get("name")),
        "duration_ms": span.get("duration_ms"),
        "status": span.get("status"),
        "downstream_service": _redact_public_text(span.get("downstream_service")),
    }


def _redact_exception(exc: BaseException) -> str:
    return redact_text(str(exc)).redacted_text


def _redact_query_text(value: str) -> str:
    return redact_text(value).redacted_text


def _redact_public_text(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value).redacted_text
    return value


def _redact_trace_identifier(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    result = redact_text(value)
    # Trace/span IDs can look like opaque tokens. Preserve raw-token-shaped IDs
    # while still redacting explicit keyed secrets or internal endpoints.
    if any(redaction_type != "raw_token" for redaction_type in result.redaction_types):
        return result.redacted_text
    return value
