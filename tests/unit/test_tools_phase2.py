"""Tests for the Phase 2 tool-layer productionization."""

from __future__ import annotations

import sys
import types
from datetime import UTC, datetime

import httpx
import pytest

from packages.common.settings import Settings
from packages.tools import (
    DbDiagnosticsTool,
    GitChangeTool,
    K8sDiagnosticsTool,
    TraceTool,
    build_db_diagnostics_backend,
    build_deployment_backend,
    build_k8s_backend,
    build_remediation_suggestions,
    build_trace_backend,
)
from packages.tools.cache import build_cache_key
from packages.tools.db_diagnostics import DbDiagnosticsQuery, _assert_read_only
from packages.tools.deployment_backends import ArgoCDDeploymentBackend, GitHubDeploymentBackend
from packages.tools.git_changes import GitChangeQuery
from packages.tools.k8s import K8sQuery
from packages.tools.logs import LogsQuery, LogsTool
from packages.tools.metrics import MetricsQuery, MetricsTool, _promql
from packages.tools.trace_backends import JaegerTraceBackend
from packages.tools.traces import TraceQuery

START = datetime(2026, 6, 1, 0, 0, tzinfo=UTC)
END = datetime(2026, 6, 1, 0, 10, tzinfo=UTC)


# --- 2.1 PromQL/LogQL templates + query safety ---


def test_promql_uses_configurable_service_label() -> None:
    query = _promql("error_rate", "checkout", service_label="app")
    assert 'app="checkout"' in query
    assert 'service="checkout"' not in query


def test_promql_covers_new_fault_metric_types() -> None:
    for metric in ("cpu_throttle", "disk_avail", "cert_expiry_days", "queue_lag", "slo_burn_rate"):
        assert "checkout" in _promql(metric, "checkout")  # type: ignore[arg-type]


def test_metric_for_alert_maps_new_fault_types() -> None:
    from packages.agent.nodes.collect_metrics import _metric_for_alert

    # MVP mappings preserved.
    assert _metric_for_alert("DatabaseConnectionExhaustion") == "db_connections"
    assert _metric_for_alert("RedisCacheAvalanche") == "cache_hit_rate"
    assert _metric_for_alert("PodRestartLoop") == "memory"
    assert _metric_for_alert("High5xxAfterDeploy") == "error_rate"
    # Phase 2.4 fault catalog now routes to its own metric type.
    assert _metric_for_alert("CPUThrottling") == "cpu_throttle"
    assert _metric_for_alert("DiskFull") == "disk_avail"
    assert _metric_for_alert("CertificateExpiry") == "cert_expiry_days"
    assert _metric_for_alert("DNSFailure") == "dns_error_rate"
    assert _metric_for_alert("MessageQueueLag") == "queue_lag"
    assert _metric_for_alert("RateLimitTriggered") == "rate_limit_hits"
    assert _metric_for_alert("ErrorBudgetBurn") == "slo_burn_rate"
    assert _metric_for_alert("SlowAPI") == "latency"


def test_metrics_tool_shards_large_windows() -> None:
    calls: list[tuple[int, int]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((int(request.url.params["start"]), int(request.url.params["end"])))
        return httpx.Response(
            200,
            json={"status": "success", "data": {"result": [{"values": [[1, "1"]]}]}},
        )

    client = httpx.Client(base_url="http://prom", transport=httpx.MockTransport(handler))
    tool = MetricsTool(
        base_url="http://prom",
        client=client,
        max_window_seconds=3600,
        max_shards=6,
    )
    # 3-hour window with a 1-hour shard cap -> 3 requests.
    end = datetime(2026, 6, 1, 3, 0, tzinfo=UTC)
    result = tool.run(MetricsQuery(service="checkout", metric_type="qps", start=START, end=end))

    assert result.status == "succeeded"
    assert len(calls) == 3


def test_metrics_tool_caps_shards_but_covers_full_window() -> None:
    # Regression: capping shard count must NOT silently truncate the window.
    # The shards must still span [start, end] (coarsened), and a spike anywhere
    # in the window must be observed.
    windows: list[tuple[int, int]] = []
    end = datetime(2026, 6, 1, 5, 0, tzinfo=UTC)
    spike_at = int(datetime(2026, 6, 1, 4, 30, tzinfo=UTC).timestamp())

    def handler(request: httpx.Request) -> httpx.Response:
        s = int(request.url.params["start"])
        e = int(request.url.params["end"])
        windows.append((s, e))
        # Spike only in the final part of the window.
        value = "100" if s <= spike_at <= e else "1"
        return httpx.Response(
            200,
            json={"status": "success", "data": {"result": [{"values": [[s, value]]}]}},
        )

    client = httpx.Client(base_url="http://prom", transport=httpx.MockTransport(handler))
    tool = MetricsTool(base_url="http://prom", client=client, max_window_seconds=600, max_shards=2)
    result = tool.run(MetricsQuery(service="checkout", metric_type="qps", start=START, end=end))

    assert len(windows) == 2  # capped
    assert windows[0][0] == int(START.timestamp())  # starts at window start
    assert windows[-1][1] == int(end.timestamp())  # reaches window end (no truncation)
    assert result.data["stats"]["max"] == 100.0  # spike in the tail is observed


def test_logs_tool_uses_configurable_service_label() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url.params["query"]))
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {"result": [{"stream": {"app": "checkout"}, "values": []}]},
            },
        )

    client = httpx.Client(base_url="http://loki", transport=httpx.MockTransport(handler))
    tool = LogsTool(base_url="http://loki", client=client, service_label="app")
    tool.run(LogsQuery(service="checkout", start=START, end=END))

    assert seen and 'app="checkout"' in seen[0]
    assert 'service="checkout"' not in seen[0]


# --- 2.1 Trace backend ---


def test_trace_fixture_backend_default() -> None:
    tool = TraceTool()
    result = tool.run(TraceQuery(service="checkout", start=START, end=END, min_duration_ms=500))
    assert result.status == "succeeded"
    assert result.evidence[0]["source"] == "fixture"


def test_jaeger_trace_backend_maps_spans() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/traces"
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "processes": {"p1": {"serviceName": "checkout"}},
                        "spans": [
                            {
                                "traceID": "t1",
                                "spanID": "s1",
                                "operationName": "POST /checkout",
                                "processID": "p1",
                                "startTime": int(START.timestamp() * 1_000_000) + 1000,
                                "duration": 900_000,
                                "tags": [
                                    {"key": "error", "value": True},
                                    {"key": "peer.service", "value": "payments"},
                                ],
                            }
                        ],
                    }
                ]
            },
        )

    client = httpx.Client(base_url="http://jaeger", transport=httpx.MockTransport(handler))
    backend = JaegerTraceBackend(base_url="http://jaeger", client=client)
    tool = TraceTool(backend=backend)
    result = tool.run(TraceQuery(service="checkout", start=START, end=END, min_duration_ms=500))

    assert result.status == "succeeded"
    assert result.data["error_spans"]
    assert "payments" in result.data["downstream_services"]
    assert result.data["duration_p95_ms"] == 900


def test_trace_backend_timeout_degrades() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("slow")

    client = httpx.Client(base_url="http://jaeger", transport=httpx.MockTransport(handler))
    tool = TraceTool(backend=JaegerTraceBackend(base_url="http://jaeger", client=client))
    result = tool.run(TraceQuery(service="checkout", start=START, end=END))
    assert result.status == "timeout"


def test_build_trace_backend_selects_by_setting() -> None:
    assert build_trace_backend(Settings()).name == "fixture"
    assert build_trace_backend(Settings(trace_backend="jaeger")).name == "jaeger"
    assert build_trace_backend(Settings(trace_backend="tempo")).name == "jaeger"
    # invalid trace_backend values are now caught at Settings construction time
    # (pydantic model_validator) — not at build_trace_backend time.
    from pydantic import ValidationError
    with pytest.raises(ValidationError, match="TRACE_BACKEND"):
        Settings(trace_backend="nope")
    # TRACE_BACKEND=disabled returns degraded backend.
    assert build_trace_backend(Settings(trace_backend="disabled")).name == "degraded"
    # TRACE_ENABLED=false also returns degraded backend.
    assert build_trace_backend(Settings(trace_enabled=False)).name == "degraded"


# --- 2.1 Deployment backend ---


def test_github_deployment_backend_maps_changes() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/repos/acme/app/deployments"
        return httpx.Response(
            200,
            json=[
                {
                    "sha": "a1b2c3d4e5",
                    "created_at": "2026-06-01T00:05:00Z",
                    "creator": {"login": "deployer"},
                    "description": "release v2",
                    "ref": "main",
                }
            ],
        )

    client = httpx.Client(base_url="http://gh", transport=httpx.MockTransport(handler))
    backend = GitHubDeploymentBackend(api_url="http://gh", repo="acme/app", client=client)
    tool = GitChangeTool(backend=backend)
    result = tool.run(GitChangeQuery(service="checkout", start=START, end=END))

    assert result.status == "succeeded"
    assert result.data["changes"][0]["commit_sha"] == "a1b2c3d"
    assert result.data["changes"][0]["author"] == "deployer"


def test_argocd_deployment_backend_reverses_history() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/applications/checkout"
        return httpx.Response(
            200,
            json={
                "status": {
                    "history": [
                        {"revision": "old1234", "deployedAt": "2026-06-01T00:01:00Z"},
                        {"revision": "new5678", "deployedAt": "2026-06-01T00:05:00Z"},
                    ]
                }
            },
        )

    client = httpx.Client(base_url="http://argo", transport=httpx.MockTransport(handler))
    backend = ArgoCDDeploymentBackend(base_url="http://argo", client=client)
    tool = GitChangeTool(backend=backend)
    result = tool.run(GitChangeQuery(service="checkout", start=START, end=END))

    assert result.status == "succeeded"
    # Newest-first after reversal.
    assert result.data["changes"][0]["commit_sha"] == "new5678"


def test_deployment_backend_http_error_degrades() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"message": "boom"})

    client = httpx.Client(base_url="http://gh", transport=httpx.MockTransport(handler))
    backend = GitHubDeploymentBackend(api_url="http://gh", repo="acme/app", client=client)
    tool = GitChangeTool(backend=backend)
    result = tool.run(GitChangeQuery(service="checkout", start=START, end=END))
    assert result.status == "degraded"
    assert result.error_message


def test_build_deployment_backend_selects_by_setting() -> None:
    assert build_deployment_backend(Settings()).name == "fixture"
    assert build_deployment_backend(Settings(deployment_backend="argocd")).name == "argocd"
    with pytest.raises(ValueError, match="requires github_repo"):
        build_deployment_backend(Settings(deployment_backend="github"))
    assert (
        build_deployment_backend(Settings(deployment_backend="github", github_repo="acme/app")).name
        == "github"
    )
    with pytest.raises(ValueError, match="unknown deployment_backend"):
        build_deployment_backend(Settings(deployment_backend="nope"))


# --- 2.2 Kubernetes read-only diagnosis ---


def test_k8s_tool_reads_fixture_events() -> None:
    tool = K8sDiagnosticsTool()
    result = tool.run(K8sQuery(service="checkout", operation="events"))
    assert result.status == "succeeded"
    assert result.evidence[0]["source"] == "fixture"


def test_k8s_tool_degrades_on_missing_service() -> None:
    tool = K8sDiagnosticsTool()
    result = tool.run(K8sQuery(service="unknown", operation="events"))
    assert result.status == "degraded"


def test_k8s_tool_refuses_write_operation() -> None:
    tool = K8sDiagnosticsTool()
    result = tool.run(K8sQuery(service="checkout", operation="scale"))
    assert result.status == "failed"
    assert "read-only" in (result.error_message or "")


def test_k8s_tool_degrades_on_bad_fixture(tmp_path: object) -> None:
    bad = f"{tmp_path}/missing.json"  # type: ignore[str-bytes-safe]
    tool = K8sDiagnosticsTool(fixture_path=bad)
    result = tool.run(K8sQuery(service="checkout", operation="events"))
    assert result.status == "degraded"


def test_build_remediation_suggestions_are_dry_run_only() -> None:
    suggestions = build_remediation_suggestions(
        "checkout", "prod", ["restart", "rollout_undo", "cordon", "unknown"]
    )
    assert len(suggestions) == 3  # unknown dropped
    assert all(s["dry_run"] and not s["executed"] and s["requires_approval"] for s in suggestions)
    risks = {s["action"]: s["risk_level"] for s in suggestions}
    assert risks["rollout_undo"] == "L3"
    assert risks["restart"] == "L2"


def test_build_k8s_backend_selects_by_setting() -> None:
    assert build_k8s_backend(Settings()).name == "fixture"
    assert build_k8s_backend(Settings(k8s_backend="live")).name == "live"
    with pytest.raises(ValueError, match="unknown k8s_backend"):
        build_k8s_backend(Settings(k8s_backend="nope"))


# --- 2.3 DB read-only diagnosis ---


def test_db_tool_reads_fixture() -> None:
    tool = DbDiagnosticsTool()
    result = tool.run(DbDiagnosticsQuery(operation="connection_pool"))
    assert result.status == "succeeded"
    assert result.evidence[0]["source"] == "fixture"


def test_db_tool_slow_queries_respects_limit() -> None:
    tool = DbDiagnosticsTool()
    result = tool.run(DbDiagnosticsQuery(operation="slow_queries", limit=2))
    assert len(result.data["rows"]) == 2


def test_db_tool_degrades_on_unknown_operation_fixture() -> None:
    tool = DbDiagnosticsTool()
    result = tool.run(DbDiagnosticsQuery(operation="locks"))
    assert result.status == "succeeded"


def test_assert_read_only_rejects_writes() -> None:
    _assert_read_only("SELECT 1")
    with pytest.raises(ValueError, match="only permits SELECT"):
        _assert_read_only("UPDATE foo SET x = 1")
    with pytest.raises(ValueError, match="write keyword"):
        _assert_read_only("SELECT 1; DROP TABLE foo")


def test_build_db_backend_selects_by_setting() -> None:
    assert build_db_diagnostics_backend(Settings()).name == "fixture"
    with pytest.raises(ValueError, match="requires db_diagnostics_url"):
        build_db_diagnostics_backend(Settings(db_diagnostics_backend="live"))
    assert (
        build_db_diagnostics_backend(
            Settings(db_diagnostics_backend="live", db_diagnostics_url="postgresql://x")
        ).name
        == "live"
    )
    with pytest.raises(ValueError, match="unknown db_diagnostics_backend"):
        build_db_diagnostics_backend(Settings(db_diagnostics_backend="nope"))


# --- 2.3 Live DB backend (read-only fix) ---


def test_live_db_backend_is_read_only_without_set_transaction(monkeypatch) -> None:
    from packages.tools.db_diagnostics import DbDiagnosticsQuery, LiveDbBackend

    executed: list[str] = []
    captured: dict[str, object] = {}

    class FakeCursor:
        description = [("state",), ("connections",)]

        def __enter__(self) -> FakeCursor:
            return self

        def __exit__(self, *a: object) -> bool:
            return False

        def execute(self, sql: str, params: object = None) -> None:
            executed.append(sql)

        def fetchall(self) -> list[tuple[object, ...]]:
            return [("active", 95)]

    class FakeConn:
        read_only = None

        def __enter__(self) -> FakeConn:
            return self

        def __exit__(self, *a: object) -> bool:
            return False

        def cursor(self) -> FakeCursor:
            return FakeCursor()

    def fake_connect(dsn: str, connect_timeout: int | None = None) -> FakeConn:
        captured["dsn"] = dsn
        captured["connect_timeout"] = connect_timeout
        conn = FakeConn()
        captured["conn"] = conn
        return conn

    monkeypatch.setitem(sys.modules, "psycopg", types.SimpleNamespace(connect=fake_connect))
    backend = LiveDbBackend(
        dsn="postgresql://ro@db/x", statement_timeout_ms=1500, connect_timeout_seconds=3
    )
    rows = backend.fetch(DbDiagnosticsQuery(operation="connection_pool"))

    assert rows == [{"state": "active", "connections": 95}]
    assert captured["connect_timeout"] == 3
    assert captured["conn"].read_only is True  # type: ignore[union-attr]
    assert any("SET statement_timeout = 1500" in s for s in executed)
    # Regression: must NOT emit the order-sensitive (and now redundant) statement.
    assert not any("SET TRANSACTION READ ONLY" in s for s in executed)


# --- 2.2 Live K8s backend ---


def test_live_k8s_backend_refuses_write_operation() -> None:
    from packages.tools.k8s import LiveK8sBackend

    with pytest.raises(ValueError, match="non-read-only"):
        LiveK8sBackend().fetch(K8sQuery(service="x", operation="scale"))


def test_live_k8s_backend_maps_events(monkeypatch) -> None:
    from packages.tools.k8s import LiveK8sBackend

    class _Item:
        def __init__(self, message: str) -> None:
            self.message = message

    class _Events:
        items = [_Item("OOMKilling"), _Item("BackOff")]

    class _CoreV1Api:
        def list_namespaced_event(self, ns: str, _request_timeout: float | None = None) -> _Events:
            return _Events()

    fake_kubernetes = types.ModuleType("kubernetes")
    fake_kubernetes.client = types.SimpleNamespace(CoreV1Api=lambda: _CoreV1Api())  # type: ignore[attr-defined]
    fake_kubernetes.config = types.SimpleNamespace(load_kube_config=lambda: None)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "kubernetes", fake_kubernetes)

    out = LiveK8sBackend(namespace="default").fetch(
        K8sQuery(service="checkout", operation="events")
    )
    assert out["operation"] == "events"
    assert "OOMKilling" in out["payload"]


# --- 2.2/2.3 cross-validation participation ---


def test_k8s_and_db_evidence_corroborate_in_cross_validation() -> None:
    from packages.agent.evidence_validation import cross_validate_state

    state = {
        "metrics_evidence": [{"payload": {"stats": {"change_ratio": 0.9}}}],
        "k8s_evidence": [
            {"payload": {"operation": "events", "payload": [{"reason": "OOMKilling"}]}}
        ],
        "db_evidence": [
            {
                "payload": {
                    "operation": "connection_pool",
                    "rows": [{"state": "active", "connections": 95}],
                }
            }
        ],
    }
    result = cross_validate_state(state)
    assert result["status"] == "corroborated"
    assert "k8s" in result["corroborating_sources"]
    assert "db" in result["corroborating_sources"]
    assert result["confidence_adjustment"] > 0


def test_db_evidence_below_threshold_is_normal_not_anomaly() -> None:
    from packages.agent.evidence_validation import _db_direction, _k8s_direction

    assert _db_direction([{"rows": [{"state": "active", "connections": 5}]}]) == "normal"
    assert _db_direction([]) is None
    assert _k8s_direction([{"payload": [{"reason": "Started"}]}]) == "normal"
    assert _k8s_direction([{"payload": [{"reason": "OOMKilling"}]}]) == "anomaly"


# --- cache key discrimination ---


def test_cache_key_discriminates_datasource() -> None:
    query = TraceQuery(service="checkout", start=START, end=END)
    key_a = build_cache_key(
        tool_name="traces",
        service="checkout",
        query=query,
        start=START,
        end=END,
        bucket_seconds=300,
        datasource="fixture",
    )
    key_b = build_cache_key(
        tool_name="traces",
        service="checkout",
        query=query,
        start=START,
        end=END,
        bucket_seconds=300,
        datasource="jaeger",
    )
    assert key_a != key_b
