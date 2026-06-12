"""PR 9.5 — TempoTraceBackend tests."""

from __future__ import annotations

import httpx

from packages.common.settings import Settings
from packages.tools.trace_backends import (
    TempoTraceBackend,
    build_trace_backend,
)


class TestTempoBackendDefaultDisabled:
    def test_tempo_backend_not_selected_by_default(self):
        settings = Settings()
        backend = build_trace_backend(settings)
        assert not isinstance(backend, TempoTraceBackend)

    def test_trace_backend_tempo_opt_in(self):
        settings = Settings(trace_backend="tempo", trace_enabled=True)
        backend = build_trace_backend(settings)
        assert isinstance(backend, TempoTraceBackend)
        assert backend.name == "tempo"


class TestTempoCapabilityDetection:
    def _make_backend(self, **kwargs):
        return TempoTraceBackend(
            base_url="http://localhost:3200",
            timeout_seconds=5.0,
            **kwargs,
        )

    def test_capability_detection_defaults(self):
        backend = self._make_backend()
        caps = backend.capabilities
        assert "supports_trace_by_id" in caps
        assert "supports_search" in caps
        assert "supports_service_filter" in caps
        assert "supports_traceql" in caps

    def test_capability_trace_by_id_only(self):
        backend = self._make_backend()
        backend.set_capability("supports_search", False)
        backend.set_capability("supports_service_filter", False)
        backend.set_capability("supports_traceql", False)
        assert backend.capabilities["supports_trace_by_id"] is True
        assert backend.capabilities["supports_search"] is False

    def test_traceql_unavailable_does_not_fail_backend(self):
        backend = self._make_backend()
        backend.set_capability("supports_traceql", False)
        assert backend.capabilities["supports_trace_by_id"] is True
        assert backend.name == "tempo"


class TestTempoDegradedBehavior:
    def _make_backend(self, **kwargs):
        return TempoTraceBackend(
            base_url="http://localhost:3200",
            timeout_seconds=2.0,
            **kwargs,
        )

    def test_unavailable_degraded(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise Exception("Connection refused")

        client = httpx.Client(
            base_url="http://localhost:3200",
            transport=httpx.MockTransport(handler),
        )
        backend = self._make_backend(client=client)
        spans = backend.fetch_trace_by_id("abc123")
        assert spans == []

    def test_auth_error_degraded(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"error": "unauthorized"})

        client = httpx.Client(
            base_url="http://localhost:3200",
            transport=httpx.MockTransport(handler),
        )
        backend = self._make_backend(client=client)
        spans = backend.fetch_trace_by_id("abc123")
        assert spans == []

    def test_no_raw_secret_in_evidence(self):
        backend = self._make_backend()
        backend_repr = repr(backend)
        assert "secret" not in backend_repr.lower()


class TestTempoSpanFetch:
    def _make_backend(self, **kwargs):
        return TempoTraceBackend(
            base_url="http://localhost:3200",
            timeout_seconds=5.0,
            **kwargs,
        )

    def test_fetch_trace_by_id_success(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={
                "batches": [{
                    "resource": {
                        "attributes": [
                            {"key": "service.name",
                             "value": {"stringValue": "checkout"}},
                        ],
                    },
                    "instrumentationLibrarySpans": [{
                        "spans": [{
                            "traceId": "abc123",
                            "spanId": "span001",
                            "name": "POST /checkout",
                            "startTimeUnixNano": "1700000000000000000",
                            "endTimeUnixNano": "1700000000500000000",
                            "status": {"code": 1},
                        }],
                    }],
                }],
            })

        client = httpx.Client(
            base_url="http://localhost:3200",
            transport=httpx.MockTransport(handler),
        )
        backend = self._make_backend(client=client)
        spans = backend.fetch_trace_by_id("abc123")
        assert len(spans) > 0
        assert spans[0]["trace_id"] == "abc123"

    def test_fetch_spans_degraded_when_search_unavailable(self):
        backend = self._make_backend()
        backend.set_capability("supports_search", False)
        from datetime import UTC, datetime

        spans = backend.fetch_spans(
            "checkout",
            datetime(2026, 1, 1, tzinfo=UTC),
            datetime(2026, 1, 2, tzinfo=UTC),
        )
        assert spans == []

    def test_build_trace_backend_tempo_integration(self):
        settings = Settings(
            trace_backend="tempo",
            trace_enabled=True,
            tempo_url="http://tempo:3200",
        )
        backend = build_trace_backend(settings)
        assert isinstance(backend, TempoTraceBackend)
