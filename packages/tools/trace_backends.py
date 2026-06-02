"""Trace backends for the TraceTool (roadmap Phase 2.1).

The fixture backend preserves MVP behaviour (reads ``demo/faults/traces.json``)
and keeps tests deterministic. The Jaeger backend talks to a real Jaeger/Tempo
query API and is only contacted when ``trace_backend`` is set away from
``fixture``. All backends return raw span dicts in a single normalized shape so
``TraceTool`` keeps one analysis path:

    {trace_id, span_id, name, service, downstream_service, duration_ms,
     status, start}
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import httpx

from packages.common.settings import Settings


class TraceBackend(Protocol):
    name: str

    def fetch_spans(self, service: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
        """Return raw span dicts for the service within the window."""


class FixtureTraceBackend:
    """Reads spans from the demo fixture file (MVP-compatible default)."""

    name = "fixture"

    def __init__(self, fixture_path: str | Path = "demo/faults/traces.json") -> None:
        self.fixture_path = Path(fixture_path)

    def fetch_spans(self, service: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
        payload = json.loads(self.fixture_path.read_text(encoding="utf-8"))
        spans = payload.get("spans", [])
        if not isinstance(spans, list):
            msg = "spans must be a list"
            raise ValueError(msg)
        return [span for span in spans if isinstance(span, dict)]


class JaegerTraceBackend:
    """Queries a Jaeger-compatible trace API (Jaeger or Tempo).

    Tempo exposes a Jaeger-compatible query endpoint, so the same adapter serves
    both — only the base URL differs.
    """

    name = "jaeger"

    def __init__(
        self,
        *,
        base_url: str,
        timeout_seconds: float = 2.0,
        client: httpx.Client | None = None,
        limit: int = 200,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.client = client
        self.limit = limit

    def fetch_spans(self, service: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
        params: dict[str, str | int] = {
            "service": service,
            # Jaeger expects microseconds.
            "start": int(start.timestamp() * 1_000_000),
            "end": int(end.timestamp() * 1_000_000),
            "limit": self.limit,
        }
        if self.client is not None:
            response = self.client.get("/api/traces", params=params, timeout=self.timeout_seconds)
        else:
            with httpx.Client(base_url=self.base_url, timeout=self.timeout_seconds) as client:
                response = client.get("/api/traces", params=params)
        response.raise_for_status()
        payload: dict[str, Any] = response.json()
        return _spans_from_jaeger(payload, service)


def _spans_from_jaeger(payload: dict[str, Any], service: str) -> list[dict[str, Any]]:
    spans: list[dict[str, Any]] = []
    for trace in payload.get("data", []):
        processes = trace.get("processes", {}) if isinstance(trace, dict) else {}
        for raw in trace.get("spans", []) if isinstance(trace, dict) else []:
            if not isinstance(raw, dict):
                continue
            tags = _tag_map(raw.get("tags", []))
            process = processes.get(raw.get("processID"), {})
            span_service = process.get("serviceName") or service
            start_us = int(raw.get("startTime", 0))
            duration_us = int(raw.get("duration", 0))
            spans.append(
                {
                    "trace_id": raw.get("traceID"),
                    "span_id": raw.get("spanID"),
                    "name": raw.get("operationName"),
                    "service": span_service,
                    "downstream_service": tags.get("peer.service")
                    or tags.get("downstream.service"),
                    "duration_ms": duration_us // 1000,
                    "status": "error" if _is_error(tags) else "ok",
                    "start": datetime.fromtimestamp(start_us / 1_000_000, tz=UTC).isoformat(),
                }
            )
    return spans


def _tag_map(tags: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        str(tag.get("key")): tag.get("value")
        for tag in tags
        if isinstance(tag, dict) and tag.get("key") is not None
    }


def _is_error(tags: dict[str, Any]) -> bool:
    if tags.get("error") in (True, "true", "True"):
        return True
    status_code = tags.get("otel.status_code") or tags.get("status.code")
    if isinstance(status_code, str) and status_code.upper() == "ERROR":
        return True
    http_status = tags.get("http.status_code")
    try:
        return http_status is not None and int(http_status) >= 500
    except (TypeError, ValueError):
        return False


def build_trace_backend(settings: Settings) -> TraceBackend:
    """Select the trace backend from settings (default: fixture)."""
    backend = settings.trace_backend.strip().lower()
    if backend == "fixture":
        return FixtureTraceBackend(fixture_path=settings.trace_fixture_path)
    if backend == "jaeger":
        return JaegerTraceBackend(
            base_url=settings.jaeger_url, timeout_seconds=settings.tool_timeout_seconds
        )
    if backend == "tempo":
        return JaegerTraceBackend(
            base_url=settings.tempo_url, timeout_seconds=settings.tool_timeout_seconds
        )
    msg = f"unknown trace_backend '{settings.trace_backend}'"
    raise ValueError(msg)
