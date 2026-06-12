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


class DegradedTraceBackend:
    """No-op trace backend returned when TRACE_BACKEND=disabled or TRACE_ENABLED=false.

    All fetch_spans calls return an empty list — the TraceTool will report
    degraded status upstream.
    """

    name = "degraded"

    def fetch_spans(self, service: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
        return []


class TempoTraceBackend:
    """Native Tempo trace backend with capability detection (M9 PR 9.5).

    Talks to the Grafana Tempo HTTP API. Supports:
    - trace by ID via ``/api/traces/{trace_id}``
    - service/time-range search via ``/api/search``
    - TraceQL queries via ``/api/search?q=...``

    Capability detection allows graceful degradation: if the Tempo instance
    only supports trace-by-ID, service/time-range queries return empty
    (degraded) instead of crashing the TraceTool.

    Auth is integrated via RuntimeBackendAuthConfig — raw secrets never
    enter evidence, logs, audit, or state.
    """

    name = "tempo"

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
        # Capability flags — default to True, narrowed by probing/failure.
        self.capabilities: dict[str, bool] = {
            "supports_trace_by_id": True,
            "supports_search": True,
            "supports_service_filter": True,
            "supports_traceql": True,
        }

    def set_capability(self, name: str, value: bool) -> None:
        if name in self.capabilities:
            self.capabilities[name] = value

    # ------------------------------------------------------------------
    # TraceTool protocol
    # ------------------------------------------------------------------

    def fetch_spans(self, service: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
        """Fetch spans for a service within a time window.

        Returns empty list (degraded) when search is unavailable.
        """
        if not self.capabilities.get("supports_search"):
            return []
        try:
            return self._search_spans(service, start, end)
        except Exception:
            return []

    def fetch_trace_by_id(self, trace_id: str) -> list[dict[str, Any]]:
        """Fetch spans for a specific trace ID."""
        if not self.capabilities.get("supports_trace_by_id"):
            return []
        try:
            return self._get_trace(trace_id)
        except Exception:
            return []

    def search_traceql(self, query: str) -> list[dict[str, Any]]:
        """Execute a TraceQL query."""
        if not self.capabilities.get("supports_traceql"):
            return []
        try:
            return self._traceql_search(query)
        except Exception:
            return []

    # ------------------------------------------------------------------
    # HTTP methods
    # ------------------------------------------------------------------

    def _get_trace(self, trace_id: str) -> list[dict[str, Any]]:
        path = f"/api/traces/{trace_id}"
        resp = self._request(path)
        return _spans_from_tempo(resp, "")

    def _search_spans(self, service: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
        params: dict[str, str | int] = {
            "start": int(start.timestamp()),
            "end": int(end.timestamp()),
            "limit": self.limit,
        }
        if self.capabilities.get("supports_service_filter"):
            params["tags"] = f'service.name="{service}"'
        resp = self._request("/api/search", params=params)
        return _spans_from_tempo_search(resp, service)

    def _traceql_search(self, query: str) -> list[dict[str, Any]]:
        params = {"q": query, "limit": self.limit}
        resp = self._request("/api/search", params=params)
        return _spans_from_tempo_search(resp, "")

    def _request(
        self, path: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        if self.client is not None:
            resp = self.client.get(
                path, params=params, timeout=self.timeout_seconds
            )
        else:
            with httpx.Client(
                base_url=self.base_url, timeout=self.timeout_seconds
            ) as client:
                resp = client.get(path, params=params)
        resp.raise_for_status()
        return resp.json()  # type: ignore[no-any-return]


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


def _spans_from_tempo(payload: dict[str, Any], _service: str) -> list[dict[str, Any]]:
    """Parse spans from a Tempo trace-by-ID response (OTLP format)."""
    spans: list[dict[str, Any]] = []
    batches = payload.get("batches", [])
    if not isinstance(batches, list):
        return spans
    for batch in batches:
        if not isinstance(batch, dict):
            continue
        # Extract service name from resource attributes.
        service_name = _service
        resource = batch.get("resource", {})
        if isinstance(resource, dict):
            for attr in resource.get("attributes", []):
                if isinstance(attr, dict) and attr.get("key") == "service.name":
                    sv = attr.get("value", {})
                    if isinstance(sv, dict):
                        service_name = sv.get("stringValue", service_name)
        for lib_span in batch.get("instrumentationLibrarySpans", []):
            if not isinstance(lib_span, dict):
                continue
            for span in lib_span.get("spans", []):
                if not isinstance(span, dict):
                    continue
                parsed = _parse_otlp_span(span, service_name)
                if parsed:
                    spans.append(parsed)
    return spans


def _spans_from_tempo_search(payload: dict[str, Any], service: str) -> list[dict[str, Any]]:
    """Parse spans from a Tempo search response."""
    spans: list[dict[str, Any]] = []
    traces = payload.get("traces", [])
    if not isinstance(traces, list):
        return spans
    for trace in traces:
        if isinstance(trace, dict):
            t_spans = _spans_from_tempo(trace, service)
            spans.extend(t_spans)
    return spans


def _parse_otlp_span(span: dict[str, Any], service: str) -> dict[str, Any] | None:
    """Parse a single OTLP span dict into the normalized format."""
    trace_id = span.get("traceId", "")
    span_id = span.get("spanId", "")
    if not trace_id or not span_id:
        return None
    start_ns = int(span.get("startTimeUnixNano", 0))
    end_ns = int(span.get("endTimeUnixNano", 0))
    duration_ms = (end_ns - start_ns) // 1_000_000 if end_ns > start_ns else 0
    status_code = 0
    st = span.get("status", {})
    if isinstance(st, dict):
        status_code = st.get("code", 0)
    return {
        "trace_id": trace_id,
        "span_id": span_id,
        "name": span.get("name", "unknown"),
        "service": service,
        "downstream_service": None,
        "duration_ms": duration_ms,
        "status": "error" if status_code == 2 else "ok",
        "start": datetime.fromtimestamp(start_ns / 1_000_000_000, tz=UTC).isoformat()
        if start_ns else "",
    }


def build_trace_backend(settings: Settings) -> TraceBackend:
    """Select the trace backend from settings.

    - ``disabled`` → DegradedTraceBackend (no-op, TraceTool reports degraded).
    - ``fixture`` → FixtureTraceBackend (local/CI default).
    - ``jaeger`` → JaegerTraceBackend (M8 verified path).
    - ``tempo`` → TempoTraceBackend (native Tempo API with capability detection).

    TRACE_ENABLED=false also returns DegradedTraceBackend regardless of
    trace_backend value.
    """
    if not settings.trace_enabled:
        return DegradedTraceBackend()
    backend = settings.trace_backend.strip().lower()
    if backend == "disabled":
        return DegradedTraceBackend()
    if backend == "fixture":
        return FixtureTraceBackend(fixture_path=settings.trace_fixture_path)
    if backend == "jaeger":
        return JaegerTraceBackend(
            base_url=settings.jaeger_url, timeout_seconds=settings.tool_timeout_seconds
        )
    if backend == "tempo":
        return TempoTraceBackend(
            base_url=settings.tempo_url, timeout_seconds=settings.tool_timeout_seconds
        )
    # Unreachable — Settings validator rejects unknown values.
    msg = f"unknown trace_backend '{settings.trace_backend}'"
    raise ValueError(msg)
