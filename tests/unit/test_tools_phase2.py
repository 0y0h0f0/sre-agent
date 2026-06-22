"""Tests for the Phase 2 tool-layer productionization."""

from __future__ import annotations

import json
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


def test_metrics_tool_falls_back_to_backend_metric_and_job_alias() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        query = str(request.url.params["query"])
        seen.append(query)
        values = [[1, "7"]] if "db_pool_in_use" in query and "job=~" in query else []
        return httpx.Response(
            200,
            json={"status": "success", "data": {"result": [{"values": values}]}},
        )

    client = httpx.Client(base_url="http://prom", transport=httpx.MockTransport(handler))
    tool = MetricsTool(base_url="http://prom", client=client)
    result = tool.run(
        MetricsQuery(service="task-service", metric_type="db_connections", start=START, end=END)
    )

    assert result.status == "succeeded"
    assert result.data["query"].startswith("db_pool_in_use")
    assert "job=~" in result.data["query"]
    assert "\\-" not in result.data["query"]
    assert len(seen) > 1


def test_metrics_tool_falls_back_to_sparse_latency_window() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        query = str(request.url.params["query"])
        seen.append(query)
        values = [[1, "0.25"]] if 'job="api-gateway"' in query and "[15m]" in query else []
        return httpx.Response(
            200,
            json={"status": "success", "data": {"result": [{"values": values}]}},
        )

    client = httpx.Client(base_url="http://prom", transport=httpx.MockTransport(handler))
    tool = MetricsTool(base_url="http://prom", client=client)
    result = tool.run(
        MetricsQuery(service="api-gateway", metric_type="latency", start=START, end=END)
    )

    assert result.status == "succeeded"
    assert 'job="api-gateway"' in result.data["query"]
    assert "[15m]" in result.data["query"]
    assert any("[5m]" in query for query in seen)


def test_metrics_tool_redacts_sensitive_service_before_query_and_output() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        query = str(request.url.params["query"])
        seen.append(query)
        assert "service-secret" not in query
        return httpx.Response(
            200,
            json={"status": "success", "data": {"result": [{"values": [[1, "1"]]}]}},
        )

    client = httpx.Client(base_url="http://prom", transport=httpx.MockTransport(handler))
    tool = MetricsTool(base_url="http://prom", client=client)
    result = tool.run(
        MetricsQuery(
            service="checkout token=service-secret",
            metric_type="qps",
            start=START,
            end=END,
        )
    )

    assert result.status == "succeeded"
    assert seen and "[REDACTED]" in seen[0]
    assert "service-secret" not in str(result.data)
    assert "service-secret" not in str(result.evidence)
    assert "service-secret" not in result.summary
    assert "service-secret" not in (result.cache_key or "")


def test_metrics_tool_redacts_error_messages() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("prometheus failed token=short-secret")

    client = httpx.Client(base_url="http://prom", transport=httpx.MockTransport(handler))
    tool = MetricsTool(base_url="http://prom", client=client)
    result = tool.run(MetricsQuery(service="checkout", metric_type="qps", start=START, end=END))

    assert result.status == "degraded"
    assert "[REDACTED]" in (result.error_message or "")
    assert "short-secret" not in (result.error_message or "")


def test_metrics_tool_rejects_invalid_service_label() -> None:
    with pytest.raises(ValueError, match="valid Prometheus label name"):
        MetricsTool(base_url="http://prom", service_label='service"} or up{job="prometheus')


def test_logs_tool_falls_back_to_common_service_labels() -> None:
    seen: list[str] = []
    line = json.dumps({"level": "error", "message": "db pool wait"})

    def handler(request: httpx.Request) -> httpx.Response:
        query = str(request.url.params["query"])
        seen.append(query)
        result = []
        if query.startswith('{app="task-service"}'):
            result = [
                {
                    "stream": {"app": "task-service"},
                    "values": [["1780272000000000000", line]],
                }
            ]
        return httpx.Response(200, json={"status": "success", "data": {"result": result}})

    client = httpx.Client(base_url="http://loki", transport=httpx.MockTransport(handler))
    tool = LogsTool(base_url="http://loki", client=client)
    result = tool.run(LogsQuery(service="task-service", start=START, end=END))

    assert result.status == "succeeded"
    assert result.data["samples"][0]["labels"] == {"app": "task-service"}
    assert any(query.startswith('{app="task-service"}') for query in seen)


def test_logs_tool_redacts_sensitive_log_samples() -> None:
    line = json.dumps(
        {
            "level": "error",
            "message": "db connect failed password=super-secret token=short-secret",
        }
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {
                    "result": [
                        {
                            "stream": {"service": "checkout"},
                            "values": [["1780272000000000000", line]],
                        }
                    ]
                },
            },
        )

    client = httpx.Client(base_url="http://loki", transport=httpx.MockTransport(handler))
    tool = LogsTool(base_url="http://loki", client=client)
    result = tool.run(LogsQuery(service="checkout", start=START, end=END))

    assert result.status == "succeeded"
    assert "[REDACTED]" in result.data["samples"][0]["message"]
    assert "super-secret" not in str(result.data)
    assert "short-secret" not in str(result.data)
    assert "super-secret" not in str(result.evidence)
    assert "short-secret" not in str(result.evidence)


def test_logs_tool_redacts_sensitive_query_and_labels() -> None:
    seen: list[str] = []
    line = json.dumps({"level": "error", "message": "upstream failed"})

    def handler(request: httpx.Request) -> httpx.Response:
        query = str(request.url.params["query"])
        seen.append(query)
        assert "service-secret" not in query
        assert "keyword-secret" not in query
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {
                    "result": [
                        {
                            "stream": {"service": "checkout token=label-secret"},
                            "values": [["1780272000000000000", line]],
                        }
                    ]
                },
            },
        )

    client = httpx.Client(base_url="http://loki", transport=httpx.MockTransport(handler))
    tool = LogsTool(base_url="http://loki", client=client)
    result = tool.run(
        LogsQuery(
            service="checkout token=service-secret",
            start=START,
            end=END,
            keywords=["password=keyword-secret"],
        )
    )

    assert result.status == "succeeded"
    assert seen and "[REDACTED]" in seen[0]
    assert "[REDACTED]" in result.data["samples"][0]["labels"]["service"]
    assert "service-secret" not in str(result.data)
    assert "keyword-secret" not in str(result.data)
    assert "label-secret" not in str(result.data)
    assert "service-secret" not in str(result.evidence)
    assert "keyword-secret" not in str(result.evidence)
    assert "label-secret" not in str(result.evidence)
    assert "service-secret" not in result.summary
    assert "service-secret" not in (result.cache_key or "")


def test_logs_tool_redacts_error_messages() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connect failed token=short-secret")

    client = httpx.Client(base_url="http://loki", transport=httpx.MockTransport(handler))
    tool = LogsTool(base_url="http://loki", client=client)
    result = tool.run(LogsQuery(service="checkout", start=START, end=END))

    assert result.status == "degraded"
    assert "[REDACTED]" in (result.error_message or "")
    assert "short-secret" not in (result.error_message or "")


def test_logs_tool_rejects_invalid_service_label() -> None:
    with pytest.raises(ValueError, match="valid Loki label name"):
        LogsTool(base_url="http://loki", service_label='service"} |= "token')


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


def test_trace_tool_redacts_public_span_fields() -> None:
    class SensitiveTraceBackend:
        name = "sensitive"

        def fetch_spans(
            self,
            service: str,
            start: datetime,
            end: datetime,
        ) -> list[dict[str, object]]:
            return [
                {
                    "trace_id": "trace-1",
                    "span_id": "span-1",
                    "name": "POST /checkout password=super-secret",
                    "service": service,
                    "downstream_service": "payments token=downstream-secret",
                    "duration_ms": 900,
                    "status": "error",
                    "start": START.isoformat(),
                }
            ]

    tool = TraceTool(backend=SensitiveTraceBackend())
    result = tool.run(TraceQuery(service="checkout", start=START, end=END))

    assert result.status == "succeeded"
    assert result.data["error_spans"][0]["trace_id"] == "trace-1"
    assert "[REDACTED]" in result.data["error_spans"][0]["name"]
    assert "[REDACTED]" in result.data["error_spans"][0]["downstream_service"]
    assert "[REDACTED]" in result.data["downstream_services"][0]
    assert "super-secret" not in str(result.data)
    assert "downstream-secret" not in str(result.data)
    assert "super-secret" not in str(result.evidence)
    assert "downstream-secret" not in str(result.evidence)


def test_trace_tool_redacts_sensitive_service_before_backend_and_output() -> None:
    seen: list[str] = []

    class SensitiveServiceTraceBackend:
        name = "sensitive"

        def fetch_spans(
            self,
            service: str,
            start: datetime,
            end: datetime,
        ) -> list[dict[str, object]]:
            seen.append(service)
            assert "service-secret" not in service
            return [
                {
                    "trace_id": "trace-1",
                    "span_id": "span-1",
                    "name": "GET /checkout",
                    "service": service,
                    "downstream_service": "payments",
                    "duration_ms": 900,
                    "status": "ok",
                    "start": START.isoformat(),
                }
            ]

    tool = TraceTool(backend=SensitiveServiceTraceBackend())
    result = tool.run(
        TraceQuery(service="checkout token=service-secret", start=START, end=END)
    )

    assert result.status == "succeeded"
    assert seen and "[REDACTED]" in seen[0]
    assert "service-secret" not in result.summary
    assert "service-secret" not in str(result.data)
    assert "service-secret" not in str(result.evidence)
    assert "service-secret" not in (result.cache_key or "")


def test_trace_tool_redacts_error_messages() -> None:
    class BrokenTraceBackend:
        name = "broken"

        def fetch_spans(
            self,
            service: str,
            start: datetime,
            end: datetime,
        ) -> list[dict[str, object]]:
            raise httpx.ConnectError("connect failed token=short-secret")

    tool = TraceTool(backend=BrokenTraceBackend())
    result = tool.run(TraceQuery(service="checkout", start=START, end=END))

    assert result.status == "degraded"
    assert "[REDACTED]" in (result.error_message or "")
    assert "short-secret" not in (result.error_message or "")


def test_build_trace_backend_selects_by_setting() -> None:
    assert build_trace_backend(Settings()).name == "fixture"
    assert build_trace_backend(Settings(trace_backend="jaeger")).name == "jaeger"
    overridden = build_trace_backend(
        Settings(trace_backend="jaeger"),
        base_url="http://effective-jaeger:16686",
    )
    assert isinstance(overridden, JaegerTraceBackend)
    assert overridden.base_url == "http://effective-jaeger:16686"
    assert (
        build_trace_backend(Settings(trace_backend="tempo", trace_enabled=True)).name
        == "degraded"
    )
    assert build_trace_backend(
        Settings(
            trace_backend="tempo",
            trace_enabled=True,
            m9_extensions_enabled=True,
        )
    ).name == "tempo"
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


def test_github_deployment_backend_falls_back_to_service_commits() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path == "/repos/acme/app/deployments":
            assert request.url.params["environment"] == "task-service"
            return httpx.Response(200, json=[])
        if request.url.path == "/repos/acme/app/commits":
            return httpx.Response(
                200,
                json=[
                    {
                        "sha": "abcdef123456",
                        "commit": {"committer": {"date": "2026-06-01T00:05:00Z"}},
                    },
                    {
                        "sha": "999999999999",
                        "commit": {"committer": {"date": "2026-06-01T00:06:00Z"}},
                    },
                ],
            )
        if request.url.path == "/repos/acme/app/commits/abcdef123456":
            return httpx.Response(
                200,
                json={
                    "sha": "abcdef123456",
                    "author": {"login": "deployer"},
                    "commit": {
                        "message": "update task service",
                        "committer": {"date": "2026-06-01T00:05:00Z"},
                    },
                    "files": [
                        {"filename": "internal/task/biz/task.go"},
                        {"filename": "configs/docker/task-service.yaml"},
                    ],
                },
            )
        if request.url.path == "/repos/acme/app/commits/999999999999":
            return httpx.Response(
                200,
                json={
                    "sha": "999999999999",
                    "commit": {
                        "message": "update user service",
                        "committer": {"date": "2026-06-01T00:06:00Z"},
                    },
                    "files": [{"filename": "internal/user/biz/user.go"}],
                },
            )
        return httpx.Response(404, json={"message": "not found"})

    client = httpx.Client(base_url="http://gh", transport=httpx.MockTransport(handler))
    backend = GitHubDeploymentBackend(api_url="http://gh", repo="acme/app", client=client)
    tool = GitChangeTool(backend=backend)
    result = tool.run(GitChangeQuery(service="task-service", start=START, end=END))

    assert result.status == "succeeded"
    assert result.data["change_count"] == 1
    assert result.data["changes"][0]["commit_sha"] == "abcdef1"
    assert result.data["changes"][0]["author"] == "deployer"
    assert "internal/task/biz/task.go" in result.data["changes"][0]["files"]
    assert "/repos/acme/app/commits" in seen


def test_deployment_backend_http_error_degrades() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"message": "boom"})

    client = httpx.Client(base_url="http://gh", transport=httpx.MockTransport(handler))
    backend = GitHubDeploymentBackend(api_url="http://gh", repo="acme/app", client=client)
    tool = GitChangeTool(backend=backend)
    result = tool.run(GitChangeQuery(service="checkout", start=START, end=END))
    assert result.status == "degraded"
    assert result.error_message


def test_git_change_tool_redacts_change_values_from_backend() -> None:
    class SensitiveDeploymentBackend:
        name = "sensitive"

        def fetch_changes(
            self,
            service: str,
            start: datetime,
            end: datetime,
        ) -> list[dict[str, object]]:
            return [
                {
                    "service": service,
                    "deployed_at": START.isoformat(),
                    "commit_sha": "1234567890abcdef1234567890abcdef12345678",
                    "author": "deployer token=author-secret",
                    "summary": "deploy checkout password=super-secret",
                    "files": [
                        "apps/checkout/config/token=path-secret.yaml",
                        {"filename": "ignored-extra-shape"},
                    ],
                    "raw_backend_payload": "token=must-not-leak",
                }
            ]

    tool = GitChangeTool(backend=SensitiveDeploymentBackend())
    result = tool.run(GitChangeQuery(service="checkout", start=START, end=END))

    assert result.status == "succeeded"
    assert result.data["changes"][0]["commit_sha"] == "1234567890abcdef1234567890abcdef12345678"
    assert "[REDACTED]" in result.data["changes"][0]["author"]
    assert "[REDACTED]" in result.data["changes"][0]["summary"]
    assert "[REDACTED]" in result.data["changes"][0]["files"][0]
    assert "author-secret" not in str(result.data)
    assert "super-secret" not in str(result.data)
    assert "path-secret" not in str(result.data)
    assert "must-not-leak" not in str(result.data)
    assert "ignored-extra-shape" not in str(result.data)
    assert "author-secret" not in str(result.evidence)
    assert "super-secret" not in str(result.evidence)
    assert "path-secret" not in str(result.evidence)
    assert "must-not-leak" not in str(result.evidence)
    assert "ignored-extra-shape" not in str(result.evidence)


def test_git_change_tool_redacts_sensitive_service_before_backend_and_output() -> None:
    seen: list[str] = []

    class SensitiveServiceDeploymentBackend:
        name = "sensitive"

        def fetch_changes(
            self,
            service: str,
            start: datetime,
            end: datetime,
        ) -> list[dict[str, object]]:
            seen.append(service)
            assert "service-secret" not in service
            return [
                {
                    "service": service,
                    "deployed_at": START.isoformat(),
                    "commit_sha": "abcdef123456",
                    "author": "deployer",
                    "summary": "deploy checkout",
                    "files": ["apps/checkout/app.py"],
                }
            ]

    tool = GitChangeTool(backend=SensitiveServiceDeploymentBackend())
    result = tool.run(
        GitChangeQuery(service="checkout token=service-secret", start=START, end=END)
    )

    assert result.status == "succeeded"
    assert seen and "[REDACTED]" in seen[0]
    assert "service-secret" not in result.summary
    assert "service-secret" not in str(result.data)
    assert "service-secret" not in str(result.evidence)
    assert "service-secret" not in (result.cache_key or "")


def test_git_change_tool_redacts_error_messages() -> None:
    class BrokenDeploymentBackend:
        name = "broken"

        def fetch_changes(
            self,
            service: str,
            start: datetime,
            end: datetime,
        ) -> list[dict[str, object]]:
            raise httpx.ConnectError("github failed token=short-secret")

    tool = GitChangeTool(backend=BrokenDeploymentBackend())
    result = tool.run(GitChangeQuery(service="checkout", start=START, end=END))

    assert result.status == "degraded"
    assert "[REDACTED]" in (result.error_message or "")
    assert "short-secret" not in (result.error_message or "")


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


def test_k8s_tool_redacts_sensitive_operation_in_refusal_summary() -> None:
    tool = K8sDiagnosticsTool()
    result = tool.run(K8sQuery(service="checkout", operation="scale token=op-secret"))

    assert result.status == "failed"
    assert "[REDACTED]" in result.summary
    assert "op-secret" not in result.summary
    assert "read-only" in (result.error_message or "")


def test_k8s_tool_uses_backend_namespace_when_query_omits_it() -> None:
    captured: dict[str, str] = {}

    class _Backend:
        name = "live"
        namespace = "payments"

        def fetch(self, query: K8sQuery) -> dict[str, object]:
            captured["namespace"] = query.namespace
            return {
                "operation": query.operation,
                "payload": {"namespace": query.namespace},
            }

    tool = K8sDiagnosticsTool(backend=_Backend())
    result = tool.run(K8sQuery(service="checkout", operation="events"))

    assert result.status == "succeeded"
    assert captured["namespace"] == "payments"
    assert result.data["payload"]["namespace"] == "payments"


def test_k8s_tool_defaults_namespace_when_backend_namespace_is_blank() -> None:
    captured: dict[str, str] = {}

    class _Backend:
        name = "live"
        namespace = "   "

        def fetch(self, query: K8sQuery) -> dict[str, object]:
            captured["namespace"] = query.namespace
            return {
                "operation": query.operation,
                "payload": {"namespace": query.namespace},
            }

    tool = K8sDiagnosticsTool(backend=_Backend())
    result = tool.run(K8sQuery(service="checkout", operation="events"))

    assert result.status == "succeeded"
    assert captured["namespace"] == "default"
    assert result.data["payload"]["namespace"] == "default"


def test_k8s_tool_redacts_sensitive_service_and_namespace_before_backend() -> None:
    captured: dict[str, str] = {}

    class _Backend:
        name = "live"
        namespace = "payments token=namespace-secret"

        def fetch(self, query: K8sQuery) -> dict[str, object]:
            captured["service"] = query.service
            captured["namespace"] = query.namespace
            assert "service-secret" not in query.service
            assert "namespace-secret" not in query.namespace
            return {
                "operation": query.operation,
                "payload": {
                    "service": query.service,
                    "namespace": query.namespace,
                },
            }

    tool = K8sDiagnosticsTool(backend=_Backend())
    result = tool.run(
        K8sQuery(service="checkout token=service-secret", operation="events")
    )

    assert result.status == "succeeded"
    assert "[REDACTED]" in captured["service"]
    assert "[REDACTED]" in captured["namespace"]
    assert "service-secret" not in result.summary
    assert "namespace-secret" not in result.summary
    assert "service-secret" not in str(result.data)
    assert "namespace-secret" not in str(result.data)
    assert "service-secret" not in str(result.evidence)
    assert "namespace-secret" not in str(result.evidence)


def test_k8s_tool_degrades_on_backend_error_payload() -> None:
    class _Backend:
        name = "live"
        namespace = "payments"

        def fetch(self, query: K8sQuery) -> dict[str, object]:
            return {"operation": query.operation, "payload": {"error": "not_found"}}

    tool = K8sDiagnosticsTool(backend=_Backend())
    result = tool.run(K8sQuery(service="checkout", operation="get_deployment"))

    assert result.status == "degraded"
    assert result.evidence == []
    assert result.error_message == "k8s backend returned error: not_found"


def test_k8s_tool_redacts_unhandled_backend_exception() -> None:
    class _Backend:
        name = "live"
        namespace = "payments"

        def fetch(self, query: K8sQuery) -> dict[str, object]:
            raise RuntimeError("client failed token=short-secret")

    tool = K8sDiagnosticsTool(backend=_Backend())
    result = tool.run(K8sQuery(service="checkout", operation="events"))

    assert result.status == "degraded"
    assert "[REDACTED]" in (result.error_message or "")
    assert "short-secret" not in (result.error_message or "")


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


def test_build_k8s_backend_live_normalizes_namespace() -> None:
    backend = build_k8s_backend(Settings(k8s_backend=" LIVE ", k8s_namespace=" payments "))
    assert backend.name == "live"
    assert backend.namespace == "payments"

    blank_backend = build_k8s_backend(Settings(k8s_backend="live", k8s_namespace=" "))
    assert blank_backend.name == "live"
    assert blank_backend.namespace == "default"


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


def test_db_tool_degrades_when_backend_query_fails() -> None:
    class BrokenDbBackend:
        name = "live"

        def fetch(self, query: DbDiagnosticsQuery) -> list[dict[str, object]]:
            raise RuntimeError("pg_stat_statements missing")

    tool = DbDiagnosticsTool(backend=BrokenDbBackend())
    result = tool.run(DbDiagnosticsQuery(operation="slow_queries"))

    assert result.status == "degraded"
    assert result.data == {}
    assert result.error_message == "pg_stat_statements missing"


def test_db_tool_redacts_backend_exception() -> None:
    class BrokenDbBackend:
        name = "live"

        def fetch(self, query: DbDiagnosticsQuery) -> list[dict[str, object]]:
            raise RuntimeError("query failed password=super-secret")

    tool = DbDiagnosticsTool(backend=BrokenDbBackend())
    result = tool.run(DbDiagnosticsQuery(operation="slow_queries"))

    assert result.status == "degraded"
    assert "[REDACTED]" in (result.error_message or "")
    assert "super-secret" not in (result.error_message or "")


def test_db_tool_redacts_row_values_from_backend() -> None:
    class SensitiveDbBackend:
        name = "live"

        def fetch(self, query: DbDiagnosticsQuery) -> list[dict[str, object]]:
            return [
                {
                    "query": "SELECT * FROM users WHERE password='super-secret'",
                    "calls": 1,
                }
            ]

    tool = DbDiagnosticsTool(backend=SensitiveDbBackend())
    result = tool.run(DbDiagnosticsQuery(operation="slow_queries"))

    assert result.status == "succeeded"
    assert "[REDACTED]" in result.data["rows"][0]["query"]
    assert "super-secret" not in str(result.data)
    assert "super-secret" not in str(result.evidence)


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


def test_live_db_backend_normalizes_runtime_timeouts(monkeypatch) -> None:
    from packages.tools.db_diagnostics import DbDiagnosticsQuery, LiveDbBackend

    captured: dict[str, object] = {}
    executed: list[str] = []

    class FakeCursor:
        description = [("state",), ("connections",)]

        def __enter__(self) -> FakeCursor:
            return self

        def __exit__(self, *a: object) -> bool:
            return False

        def execute(self, sql: str, params: object = None) -> None:
            executed.append(sql)

        def fetchall(self) -> list[tuple[object, ...]]:
            return [("active", 1)]

    class FakeConn:
        read_only = None

        def __enter__(self) -> FakeConn:
            return self

        def __exit__(self, *a: object) -> bool:
            return False

        def cursor(self) -> FakeCursor:
            return FakeCursor()

    def fake_connect(dsn: str, connect_timeout: int | None = None) -> FakeConn:
        captured["connect_timeout"] = connect_timeout
        return FakeConn()

    monkeypatch.setitem(sys.modules, "psycopg", types.SimpleNamespace(connect=fake_connect))
    backend = LiveDbBackend(
        dsn="postgresql://ro@db/x",
        statement_timeout_ms="1500",  # type: ignore[arg-type]
        connect_timeout_seconds=0.5,
    )

    rows = backend.fetch(DbDiagnosticsQuery(operation="connection_pool"))

    assert rows == [{"state": "active", "connections": 1}]
    assert backend.statement_timeout_ms == 1500
    assert backend.connect_timeout_seconds == 1
    assert captured["connect_timeout"] == 1
    assert executed == [
        "SET statement_timeout = 1500",
        "SELECT state, count(*) AS connections FROM pg_stat_activity "
        "GROUP BY state ORDER BY connections DESC",
    ]


def test_live_db_backend_rejects_invalid_runtime_timeouts() -> None:
    from packages.tools.db_diagnostics import LiveDbBackend

    invalid_statement_timeouts = [0, -1, True, "1.5", "many"]
    for value in invalid_statement_timeouts:
        with pytest.raises(ValueError, match="statement_timeout_ms"):
            LiveDbBackend(dsn="postgresql://ro@db/x", statement_timeout_ms=value)  # type: ignore[arg-type]

    invalid_connect_timeouts = [0, -1, True, "many"]
    for value in invalid_connect_timeouts:
        with pytest.raises(ValueError, match="connect_timeout_seconds"):
            LiveDbBackend(dsn="postgresql://ro@db/x", connect_timeout_seconds=value)  # type: ignore[arg-type]


def test_live_db_backend_redacts_direct_query_failures(monkeypatch) -> None:
    from packages.tools.db_diagnostics import DbDiagnosticsQuery, LiveDbBackend

    def fake_connect(dsn: str, connect_timeout: int | None = None) -> object:
        raise RuntimeError(
            "could not connect password=super-secret "
            "postgresql://user:super-secret@db/prod"
        )

    monkeypatch.setitem(sys.modules, "psycopg", types.SimpleNamespace(connect=fake_connect))
    backend = LiveDbBackend(
        dsn="postgresql://user:super-secret@db/prod",
        statement_timeout_ms=1500,
        connect_timeout_seconds=3,
    )

    with pytest.raises(RuntimeError) as exc_info:
        backend.fetch(DbDiagnosticsQuery(operation="connection_pool"))

    message = str(exc_info.value)
    assert "[REDACTED]" in message
    assert "super-secret" not in message


def test_live_db_backend_redacts_row_values(monkeypatch) -> None:
    from packages.tools.db_diagnostics import DbDiagnosticsQuery, LiveDbBackend

    class FakeCursor:
        description = [("query",), ("calls",)]

        def __enter__(self) -> FakeCursor:
            return self

        def __exit__(self, *a: object) -> bool:
            return False

        def execute(self, sql: str, params: object = None) -> None:
            return None

        def fetchall(self) -> list[tuple[object, ...]]:
            return [("SELECT password='super-secret'", 12)]

    class FakeConn:
        read_only = None

        def __enter__(self) -> FakeConn:
            return self

        def __exit__(self, *a: object) -> bool:
            return False

        def cursor(self) -> FakeCursor:
            return FakeCursor()

    monkeypatch.setitem(
        sys.modules,
        "psycopg",
        types.SimpleNamespace(connect=lambda *a, **kw: FakeConn()),
    )
    backend = LiveDbBackend(
        dsn="postgresql://ro@db/x", statement_timeout_ms=1500, connect_timeout_seconds=3
    )
    rows = backend.fetch(DbDiagnosticsQuery(operation="slow_queries"))

    assert "[REDACTED]" in rows[0]["query"]
    assert "super-secret" not in str(rows)


# --- 2.2 Live K8s backend ---


def test_live_k8s_backend_refuses_write_operation() -> None:
    from packages.tools.k8s import LiveK8sBackend

    with pytest.raises(ValueError, match="non-read-only"):
        LiveK8sBackend().fetch(K8sQuery(service="x", operation="scale"))


def test_live_k8s_backend_normalizes_namespace_and_honors_query_override(
    monkeypatch,
) -> None:
    from packages.tools.k8s import LiveK8sBackend

    class _AppsV1Api:
        def __init__(self) -> None:
            self.namespaces: list[str] = []

        def read_namespaced_deployment(
            self,
            name: str,
            namespace: str,
            _request_timeout: float | None = None,
        ):
            self.namespaces.append(namespace)
            return types.SimpleNamespace(
                metadata=types.SimpleNamespace(annotations={}),
                spec=types.SimpleNamespace(
                    replicas=1,
                    paused=False,
                    template=types.SimpleNamespace(spec=types.SimpleNamespace(containers=[])),
                ),
                status=types.SimpleNamespace(),
            )

    apps = _AppsV1Api()
    fake_kubernetes = types.ModuleType("kubernetes")
    fake_kubernetes.client = types.SimpleNamespace(  # type: ignore[attr-defined]
        CoreV1Api=lambda: object(),
        AppsV1Api=lambda: apps,
    )
    fake_kubernetes.config = types.SimpleNamespace(  # type: ignore[attr-defined]
        load_incluster_config=lambda: None,
        load_kube_config=lambda: None,
    )
    monkeypatch.setitem(sys.modules, "kubernetes", fake_kubernetes)

    backend = LiveK8sBackend(namespace=" payments ")
    first = backend.fetch(K8sQuery(service="checkout", operation="get_deployment"))
    second = backend.fetch(
        K8sQuery(service="checkout", operation="get_deployment", namespace=" shipping ")
    )
    blank = LiveK8sBackend(namespace=" ").fetch(
        K8sQuery(service="checkout", operation="get_deployment")
    )

    assert apps.namespaces == ["payments", "shipping", "default"]
    assert first["payload"]["namespace"] == "payments"
    assert second["payload"]["namespace"] == "shipping"
    assert blank["payload"]["namespace"] == "default"


def test_live_k8s_backend_rejects_invalid_namespace_before_client_init() -> None:
    from packages.tools.k8s import LiveK8sBackend

    out = LiveK8sBackend(namespace="_payments").fetch(
        K8sQuery(service="checkout", operation="get_deployment")
    )

    assert out == {"operation": "get_deployment", "payload": {"error": "invalid_namespace"}}


def test_live_k8s_backend_rejects_invalid_workload_name_before_client_init() -> None:
    from packages.tools.k8s import LiveK8sBackend

    out = LiveK8sBackend(namespace="payments").fetch(
        K8sQuery(service="../checkout", operation="rollout_status")
    )

    assert out == {"operation": "rollout_status", "payload": {"error": "invalid_resource_name"}}


def test_live_k8s_backend_rejects_invalid_explicit_pod_name_before_client_init() -> None:
    from packages.tools.k8s import LiveK8sBackend

    out = LiveK8sBackend(namespace="payments").fetch(
        K8sQuery(service="checkout", operation="logs", pod="../checkout")
    )

    assert out == {"operation": "logs", "payload": {"error": "invalid_pod_name"}}


def test_live_k8s_backend_maps_events(monkeypatch) -> None:
    from packages.tools.k8s import LiveK8sBackend

    class _Item:
        def __init__(self, message: str) -> None:
            self.message = message

    class _Events:
        def __init__(self, items: list[_Item]) -> None:
            self.items = items

    class _PodList:
        def __init__(self, items: list[object]) -> None:
            self.items = items

    class _CoreV1Api:
        def __init__(self) -> None:
            self.event_selectors: list[str] = []
            self.pod_selectors: list[str] = []

        def list_namespaced_event(
            self,
            ns: str,
            field_selector: str,
            _request_timeout: float | None = None,
        ) -> _Events:
            self.event_selectors.append(field_selector)
            if field_selector == "involvedObject.name=checkout":
                return _Events([_Item("Deployment rollout started")])
            if field_selector == "involvedObject.name=checkout-running":
                return _Events([_Item("OOMKilling"), _Item("BackOff"), _Item("BackOff")])
            return _Events([])

        def list_namespaced_pod(
            self,
            namespace: str,
            label_selector: str,
            limit: int,
            _request_timeout: float | None = None,
        ) -> _PodList:
            self.pod_selectors.append(label_selector)
            if label_selector == "app=checkout":
                return _PodList(
                    [
                        types.SimpleNamespace(
                            metadata=types.SimpleNamespace(name="checkout-running"),
                            status=types.SimpleNamespace(phase="Running"),
                        )
                    ]
                )
            return _PodList([])

    calls: list[str] = []
    core = _CoreV1Api()
    fake_kubernetes = types.ModuleType("kubernetes")
    fake_kubernetes.client = types.SimpleNamespace(CoreV1Api=lambda: core)  # type: ignore[attr-defined]
    fake_kubernetes.config = types.SimpleNamespace(  # type: ignore[attr-defined]
        load_incluster_config=lambda: calls.append("incluster"),
        load_kube_config=lambda: calls.append("kubeconfig"),
    )
    monkeypatch.setitem(sys.modules, "kubernetes", fake_kubernetes)

    out = LiveK8sBackend(namespace="default").fetch(
        K8sQuery(service="checkout", operation="events")
    )
    assert out["operation"] == "events"
    assert "Deployment rollout started" in out["payload"]
    assert "OOMKilling" in out["payload"]
    assert out["payload"].count("BackOff") == 1
    assert core.event_selectors == [
        "involvedObject.name=checkout",
        "involvedObject.name=checkout-running",
    ]
    assert core.pod_selectors == ["app.kubernetes.io/name=checkout", "app=checkout"]
    assert calls == ["incluster"]


def test_live_k8s_backend_events_redacts_sensitive_messages(monkeypatch) -> None:
    from packages.tools.k8s import LiveK8sBackend

    class _Item:
        def __init__(self, message: str) -> None:
            self.message = message

    class _Events:
        def __init__(self, items: list[_Item]) -> None:
            self.items = items

    class _CoreV1Api:
        def list_namespaced_event(
            self,
            ns: str,
            field_selector: str,
            _request_timeout: float | None = None,
        ) -> _Events:
            if field_selector == "involvedObject.name=checkout-pod":
                return _Events(
                    [
                        _Item(
                            "failed auth token=short-secret "
                            "Authorization: Bearer abcdefghijklmnop"
                        )
                    ]
                )
            return _Events([])

    fake_kubernetes = types.ModuleType("kubernetes")
    fake_kubernetes.client = types.SimpleNamespace(  # type: ignore[attr-defined]
        CoreV1Api=lambda: _CoreV1Api(),
        AppsV1Api=lambda: object(),
    )
    fake_kubernetes.config = types.SimpleNamespace(  # type: ignore[attr-defined]
        load_incluster_config=lambda: None,
        load_kube_config=lambda: None,
    )
    monkeypatch.setitem(sys.modules, "kubernetes", fake_kubernetes)

    out = LiveK8sBackend(namespace="payments").fetch(
        K8sQuery(service="checkout", operation="events", pod="checkout-pod")
    )

    assert out["operation"] == "events"
    payload_text = " ".join(out["payload"])
    assert "[REDACTED]" in payload_text
    assert "short-secret" not in payload_text
    assert "abcdefghijklmnop" not in payload_text


def test_live_k8s_backend_maps_events_api_error(monkeypatch) -> None:
    from packages.tools.k8s import LiveK8sBackend

    class _ApiError(Exception):
        status = 429

        def __str__(self) -> str:
            return "rate limited token=short-secret"

    class _CoreV1Api:
        def list_namespaced_event(
            self,
            ns: str,
            field_selector: str,
            _request_timeout: float | None = None,
        ) -> object:
            raise _ApiError()

    fake_kubernetes = types.ModuleType("kubernetes")
    fake_kubernetes.client = types.SimpleNamespace(  # type: ignore[attr-defined]
        CoreV1Api=lambda: _CoreV1Api(),
        AppsV1Api=lambda: object(),
    )
    fake_kubernetes.config = types.SimpleNamespace(  # type: ignore[attr-defined]
        load_incluster_config=lambda: None,
        load_kube_config=lambda: None,
    )
    monkeypatch.setitem(sys.modules, "kubernetes", fake_kubernetes)

    out = LiveK8sBackend(namespace="payments").fetch(
        K8sQuery(service="checkout", operation="events", pod="checkout-pod")
    )

    assert out == {
        "operation": "events",
        "payload": {"error": "rate_limited", "status_code": 429},
    }
    assert "short-secret" not in str(out)


def test_live_k8s_backend_falls_back_to_kube_config(monkeypatch) -> None:
    from packages.tools.k8s import LiveK8sBackend

    class _Events:
        items: list[object] = []

    class _CoreV1Api:
        def list_namespaced_event(
            self,
            ns: str,
            field_selector: str,
            _request_timeout: float | None = None,
        ) -> _Events:
            return _Events()

        def list_namespaced_pod(
            self,
            namespace: str,
            label_selector: str,
            limit: int,
            _request_timeout: float | None = None,
        ) -> object:
            return types.SimpleNamespace(items=[])

    calls: list[str] = []

    def _raise_no_incluster() -> None:
        calls.append("incluster")
        raise RuntimeError("not running in a pod")

    fake_kubernetes = types.ModuleType("kubernetes")
    fake_kubernetes.client = types.SimpleNamespace(CoreV1Api=lambda: _CoreV1Api())  # type: ignore[attr-defined]
    fake_kubernetes.config = types.SimpleNamespace(  # type: ignore[attr-defined]
        load_incluster_config=_raise_no_incluster,
        load_kube_config=lambda: calls.append("kubeconfig"),
    )
    monkeypatch.setitem(sys.modules, "kubernetes", fake_kubernetes)

    out = LiveK8sBackend(namespace="default").fetch(
        K8sQuery(service="checkout", operation="events")
    )
    assert out["operation"] == "events"
    assert calls == ["incluster", "kubeconfig"]


def test_live_k8s_backend_redacts_config_load_failures(monkeypatch) -> None:
    from packages.tools.k8s import LiveK8sBackend

    class _CoreV1Api:
        pass

    def _raise_incluster() -> None:
        raise RuntimeError("incluster token=short-secret")

    def _raise_kubeconfig() -> None:
        raise RuntimeError("kubeconfig token=other-secret")

    fake_kubernetes = types.ModuleType("kubernetes")
    fake_kubernetes.client = types.SimpleNamespace(CoreV1Api=lambda: _CoreV1Api())  # type: ignore[attr-defined]
    fake_kubernetes.config = types.SimpleNamespace(  # type: ignore[attr-defined]
        load_incluster_config=_raise_incluster,
        load_kube_config=_raise_kubeconfig,
    )
    monkeypatch.setitem(sys.modules, "kubernetes", fake_kubernetes)

    with pytest.raises(RuntimeError) as exc_info:
        LiveK8sBackend(namespace="payments").fetch(
            K8sQuery(service="checkout", operation="events")
        )

    message = str(exc_info.value)
    assert "[REDACTED]" in message
    assert "short-secret" not in message
    assert "other-secret" not in message


def test_live_k8s_backend_logs_resolves_pod_by_common_labels(monkeypatch) -> None:
    from packages.tools.k8s import LiveK8sBackend

    class _PodList:
        def __init__(self, items: list[object]) -> None:
            self.items = items

    class _CoreV1Api:
        def __init__(self) -> None:
            self.selectors: list[str] = []

        def list_namespaced_pod(
            self,
            namespace: str,
            label_selector: str,
            limit: int,
            _request_timeout: float | None = None,
        ) -> _PodList:
            self.selectors.append(label_selector)
            if label_selector == "app=checkout":
                return _PodList(
                    [
                        types.SimpleNamespace(
                            metadata=types.SimpleNamespace(name="checkout-failed"),
                            status=types.SimpleNamespace(phase="Failed"),
                        ),
                        types.SimpleNamespace(
                            metadata=types.SimpleNamespace(name="checkout-running"),
                            status=types.SimpleNamespace(phase="Running"),
                        ),
                    ]
                )
            return _PodList([])

        def read_namespaced_pod_log(
            self,
            name: str,
            namespace: str,
            tail_lines: int,
            _request_timeout: float | None = None,
        ) -> str:
            assert name == "checkout-running"
            assert namespace == "payments"
            assert tail_lines == 100
            return "ready"

    core = _CoreV1Api()
    fake_kubernetes = types.ModuleType("kubernetes")
    fake_kubernetes.client = types.SimpleNamespace(  # type: ignore[attr-defined]
        CoreV1Api=lambda: core,
        AppsV1Api=lambda: object(),
    )
    fake_kubernetes.config = types.SimpleNamespace(  # type: ignore[attr-defined]
        load_incluster_config=lambda: None,
        load_kube_config=lambda: None,
    )
    monkeypatch.setitem(sys.modules, "kubernetes", fake_kubernetes)

    out = LiveK8sBackend(namespace="payments").fetch(
        K8sQuery(service="checkout", operation="logs")
    )

    assert out == {"operation": "logs", "pod": "checkout-running", "payload": "ready"}
    assert core.selectors == ["app.kubernetes.io/name=checkout", "app=checkout"]


def test_live_k8s_backend_logs_redacts_sensitive_text(monkeypatch) -> None:
    from packages.tools.k8s import LiveK8sBackend

    class _CoreV1Api:
        def list_namespaced_pod(self, *args: object, **kwargs: object) -> object:
            raise AssertionError("explicit pod should not trigger pod listing")

        def read_namespaced_pod_log(
            self,
            name: str,
            namespace: str,
            tail_lines: int,
            _request_timeout: float | None = None,
        ) -> str:
            assert name == "checkout-explicit"
            assert namespace == "payments"
            assert tail_lines == 100
            return (
                "connect failed password=super-secret "
                "api_key=abc123 token=short-secret"
            )

    fake_kubernetes = types.ModuleType("kubernetes")
    fake_kubernetes.client = types.SimpleNamespace(  # type: ignore[attr-defined]
        CoreV1Api=lambda: _CoreV1Api(),
        AppsV1Api=lambda: object(),
    )
    fake_kubernetes.config = types.SimpleNamespace(  # type: ignore[attr-defined]
        load_incluster_config=lambda: None,
        load_kube_config=lambda: None,
    )
    monkeypatch.setitem(sys.modules, "kubernetes", fake_kubernetes)

    out = LiveK8sBackend(namespace="payments").fetch(
        K8sQuery(service="checkout", operation="logs", pod="checkout-explicit")
    )

    assert out["operation"] == "logs"
    assert out["pod"] == "checkout-explicit"
    assert "[REDACTED]" in out["payload"]
    assert "super-secret" not in out["payload"]
    assert "abc123" not in out["payload"]
    assert "short-secret" not in out["payload"]


def test_live_k8s_backend_maps_logs_api_error(monkeypatch) -> None:
    from packages.tools.k8s import LiveK8sBackend

    class _ApiError(Exception):
        status = 504

    class _CoreV1Api:
        def list_namespaced_pod(self, *args: object, **kwargs: object) -> object:
            raise AssertionError("explicit pod should not trigger pod listing")

        def read_namespaced_pod_log(
            self,
            name: str,
            namespace: str,
            tail_lines: int,
            _request_timeout: float | None = None,
        ) -> str:
            raise _ApiError()

    fake_kubernetes = types.ModuleType("kubernetes")
    fake_kubernetes.client = types.SimpleNamespace(  # type: ignore[attr-defined]
        CoreV1Api=lambda: _CoreV1Api(),
        AppsV1Api=lambda: object(),
    )
    fake_kubernetes.config = types.SimpleNamespace(  # type: ignore[attr-defined]
        load_incluster_config=lambda: None,
        load_kube_config=lambda: None,
    )
    monkeypatch.setitem(sys.modules, "kubernetes", fake_kubernetes)

    out = LiveK8sBackend(namespace="payments").fetch(
        K8sQuery(service="checkout", operation="logs", pod="checkout-explicit")
    )

    assert out == {
        "operation": "logs",
        "pod": "checkout-explicit",
        "payload": {"error": "timeout", "status_code": 504},
    }


def test_live_k8s_backend_describe_pod_prefers_explicit_pod(monkeypatch) -> None:
    from packages.tools.k8s import LiveK8sBackend

    class _Pod:
        metadata = types.SimpleNamespace(name="checkout-explicit")
        spec = types.SimpleNamespace(
            node_name="node-a",
            containers=[
                types.SimpleNamespace(
                    name="api",
                    image="checkout:v3",
                    env=[types.SimpleNamespace(name="DB_PASSWORD", value="super-secret")],
                )
            ],
        )
        status = types.SimpleNamespace(
            phase="Running",
            container_statuses=[
                types.SimpleNamespace(
                    name="api",
                    ready=False,
                    restart_count=4,
                    state=types.SimpleNamespace(
                        waiting=types.SimpleNamespace(reason="CrashLoopBackOff")
                    ),
                )
            ],
            conditions=[types.SimpleNamespace(type="Ready", status="False")],
        )

    class _CoreV1Api:
        def list_namespaced_pod(self, *args: object, **kwargs: object) -> object:
            raise AssertionError("explicit pod should not trigger pod listing")

        def read_namespaced_pod(
            self,
            name: str,
            namespace: str,
            _request_timeout: float | None = None,
        ) -> _Pod:
            assert name == "checkout-explicit"
            assert namespace == "payments"
            return _Pod()

    fake_kubernetes = types.ModuleType("kubernetes")
    fake_kubernetes.client = types.SimpleNamespace(  # type: ignore[attr-defined]
        CoreV1Api=lambda: _CoreV1Api(),
        AppsV1Api=lambda: object(),
    )
    fake_kubernetes.config = types.SimpleNamespace(  # type: ignore[attr-defined]
        load_incluster_config=lambda: None,
        load_kube_config=lambda: None,
    )
    monkeypatch.setitem(sys.modules, "kubernetes", fake_kubernetes)

    out = LiveK8sBackend(namespace="payments").fetch(
        K8sQuery(service="checkout", operation="describe_pod", pod="checkout-explicit")
    )

    assert out == {
        "operation": "describe_pod",
        "pod": "checkout-explicit",
        "payload": {
            "name": "checkout-explicit",
            "namespace": "payments",
            "phase": "Running",
            "node_name": "node-a",
            "restart_count": 4,
            "containers": [
                {
                    "name": "api",
                    "image": "checkout:v3",
                    "ready": False,
                    "restart_count": 4,
                    "state": "waiting",
                    "reason": "CrashLoopBackOff",
                }
            ],
            "conditions": [{"type": "Ready", "status": "False"}],
        },
    }
    assert "super-secret" not in str(out)
    assert "DB_PASSWORD" not in str(out)


def test_live_k8s_backend_maps_describe_pod_api_error(monkeypatch) -> None:
    from packages.tools.k8s import LiveK8sBackend

    class _ApiError(Exception):
        status = 401

    class _CoreV1Api:
        def list_namespaced_pod(self, *args: object, **kwargs: object) -> object:
            raise AssertionError("explicit pod should not trigger pod listing")

        def read_namespaced_pod(
            self,
            name: str,
            namespace: str,
            _request_timeout: float | None = None,
        ) -> object:
            raise _ApiError()

    fake_kubernetes = types.ModuleType("kubernetes")
    fake_kubernetes.client = types.SimpleNamespace(  # type: ignore[attr-defined]
        CoreV1Api=lambda: _CoreV1Api(),
        AppsV1Api=lambda: object(),
    )
    fake_kubernetes.config = types.SimpleNamespace(  # type: ignore[attr-defined]
        load_incluster_config=lambda: None,
        load_kube_config=lambda: None,
    )
    monkeypatch.setitem(sys.modules, "kubernetes", fake_kubernetes)

    out = LiveK8sBackend(namespace="payments").fetch(
        K8sQuery(service="checkout", operation="describe_pod", pod="checkout-explicit")
    )

    assert out == {
        "operation": "describe_pod",
        "pod": "checkout-explicit",
        "payload": {"error": "unauthorized", "status_code": 401},
    }


def test_k8s_tool_degrades_when_live_pod_lookup_has_no_match(monkeypatch) -> None:
    from packages.tools.k8s import LiveK8sBackend

    class _PodList:
        items: list[object] = []

    class _CoreV1Api:
        def list_namespaced_pod(
            self,
            namespace: str,
            label_selector: str,
            limit: int,
            _request_timeout: float | None = None,
        ) -> _PodList:
            return _PodList()

    fake_kubernetes = types.ModuleType("kubernetes")
    fake_kubernetes.client = types.SimpleNamespace(  # type: ignore[attr-defined]
        CoreV1Api=lambda: _CoreV1Api(),
        AppsV1Api=lambda: object(),
    )
    fake_kubernetes.config = types.SimpleNamespace(  # type: ignore[attr-defined]
        load_incluster_config=lambda: None,
        load_kube_config=lambda: None,
    )
    monkeypatch.setitem(sys.modules, "kubernetes", fake_kubernetes)

    tool = K8sDiagnosticsTool(backend=LiveK8sBackend(namespace="payments"))
    result = tool.run(K8sQuery(service="checkout", operation="logs"))

    assert result.status == "degraded"
    assert result.error_message == "empty k8s result"


def test_live_k8s_backend_get_deployment_includes_paused(monkeypatch) -> None:
    from packages.tools.k8s import LiveK8sBackend

    class _AppsV1Api:
        def read_namespaced_deployment(
            self,
            name: str,
            namespace: str,
            _request_timeout: float | None = None,
        ):
            return types.SimpleNamespace(
                metadata=types.SimpleNamespace(
                    annotations={"deployment.kubernetes.io/revision": "8"}
                ),
                spec=types.SimpleNamespace(
                    replicas=3,
                    paused=True,
                    template=types.SimpleNamespace(
                        spec=types.SimpleNamespace(
                            containers=[types.SimpleNamespace(image="checkout:v2")]
                        )
                    ),
                ),
                status=types.SimpleNamespace(
                    ready_replicas=2,
                    available_replicas=2,
                    conditions=[types.SimpleNamespace(type="Progressing", status="True")],
                ),
            )

    fake_kubernetes = types.ModuleType("kubernetes")
    fake_kubernetes.client = types.SimpleNamespace(  # type: ignore[attr-defined]
        CoreV1Api=lambda: object(),
        AppsV1Api=lambda: _AppsV1Api(),
    )
    fake_kubernetes.config = types.SimpleNamespace(  # type: ignore[attr-defined]
        load_incluster_config=lambda: None,
        load_kube_config=lambda: None,
    )
    monkeypatch.setitem(sys.modules, "kubernetes", fake_kubernetes)

    out = LiveK8sBackend(namespace="payments").fetch(
        K8sQuery(service="checkout", operation="get_deployment")
    )

    assert out["operation"] == "get_deployment"
    assert out["payload"]["namespace"] == "payments"
    assert out["payload"]["replicas"] == 3
    assert out["payload"]["paused"] is True
    assert out["payload"]["revision"] == "8"
    assert out["payload"]["image"] == "checkout:v2"


def test_live_k8s_backend_maps_deployment_api_error_to_degraded(monkeypatch) -> None:
    from packages.tools.k8s import LiveK8sBackend

    class _ApiError(Exception):
        status = 403

        def __str__(self) -> str:
            return "forbidden token=super-secret"

    class _AppsV1Api:
        def read_namespaced_deployment(
            self,
            name: str,
            namespace: str,
            _request_timeout: float | None = None,
        ):
            raise _ApiError()

    fake_kubernetes = types.ModuleType("kubernetes")
    fake_kubernetes.client = types.SimpleNamespace(  # type: ignore[attr-defined]
        CoreV1Api=lambda: object(),
        AppsV1Api=lambda: _AppsV1Api(),
    )
    fake_kubernetes.config = types.SimpleNamespace(  # type: ignore[attr-defined]
        load_incluster_config=lambda: None,
        load_kube_config=lambda: None,
    )
    monkeypatch.setitem(sys.modules, "kubernetes", fake_kubernetes)

    backend = LiveK8sBackend(namespace="payments")
    out = backend.fetch(K8sQuery(service="checkout", operation="get_deployment"))
    tool = K8sDiagnosticsTool(backend=backend)
    result = tool.run(K8sQuery(service="checkout", operation="get_deployment"))

    assert out == {
        "operation": "get_deployment",
        "payload": {"error": "forbidden", "status_code": 403},
    }
    assert "super-secret" not in str(out)
    assert result.status == "degraded"
    assert result.evidence == []
    assert result.error_message == "k8s backend returned error: forbidden"


def test_live_k8s_backend_rollout_status_maps_complete_deployment(monkeypatch) -> None:
    from packages.tools.k8s import LiveK8sBackend

    class _AppsV1Api:
        def read_namespaced_deployment(
            self,
            name: str,
            namespace: str,
            _request_timeout: float | None = None,
        ):
            return types.SimpleNamespace(
                metadata=types.SimpleNamespace(
                    annotations={"deployment.kubernetes.io/revision": "9"},
                    generation=9,
                ),
                spec=types.SimpleNamespace(
                    replicas=3,
                    paused=False,
                    template=types.SimpleNamespace(
                        spec=types.SimpleNamespace(
                            containers=[types.SimpleNamespace(image="checkout:v3")]
                        )
                    ),
                ),
                status=types.SimpleNamespace(
                    ready_replicas=3,
                    available_replicas=3,
                    updated_replicas=3,
                    unavailable_replicas=0,
                    observed_generation=9,
                    conditions=[types.SimpleNamespace(type="Progressing", status="True")],
                ),
            )

    fake_kubernetes = types.ModuleType("kubernetes")
    fake_kubernetes.client = types.SimpleNamespace(  # type: ignore[attr-defined]
        CoreV1Api=lambda: object(),
        AppsV1Api=lambda: _AppsV1Api(),
    )
    fake_kubernetes.config = types.SimpleNamespace(  # type: ignore[attr-defined]
        load_incluster_config=lambda: None,
        load_kube_config=lambda: None,
    )
    monkeypatch.setitem(sys.modules, "kubernetes", fake_kubernetes)

    out = LiveK8sBackend(namespace="payments").fetch(
        K8sQuery(service="checkout", operation="rollout_status")
    )

    assert out["operation"] == "rollout_status"
    assert out["payload"]["namespace"] == "payments"
    assert out["payload"]["status"] == "complete"
    assert out["payload"]["desired_replicas"] == 3
    assert out["payload"]["updated_replicas"] == 3
    assert out["payload"]["available_replicas"] == 3
    assert out["payload"]["revision"] == "9"


def test_live_k8s_backend_rollout_status_maps_replica_failure(monkeypatch) -> None:
    from packages.tools.k8s import LiveK8sBackend

    class _AppsV1Api:
        def read_namespaced_deployment(
            self,
            name: str,
            namespace: str,
            _request_timeout: float | None = None,
        ):
            return types.SimpleNamespace(
                metadata=types.SimpleNamespace(annotations={}, generation=3),
                spec=types.SimpleNamespace(
                    replicas=3,
                    paused=False,
                    template=types.SimpleNamespace(
                        spec=types.SimpleNamespace(
                            containers=[types.SimpleNamespace(image="checkout:v3")]
                        )
                    ),
                ),
                status=types.SimpleNamespace(
                    ready_replicas=1,
                    available_replicas=1,
                    updated_replicas=1,
                    unavailable_replicas=2,
                    observed_generation=3,
                    conditions=[types.SimpleNamespace(type="ReplicaFailure", status="True")],
                ),
            )

    fake_kubernetes = types.ModuleType("kubernetes")
    fake_kubernetes.client = types.SimpleNamespace(  # type: ignore[attr-defined]
        CoreV1Api=lambda: object(),
        AppsV1Api=lambda: _AppsV1Api(),
    )
    fake_kubernetes.config = types.SimpleNamespace(  # type: ignore[attr-defined]
        load_incluster_config=lambda: None,
        load_kube_config=lambda: None,
    )
    monkeypatch.setitem(sys.modules, "kubernetes", fake_kubernetes)

    out = LiveK8sBackend(namespace="payments").fetch(
        K8sQuery(service="checkout", operation="rollout_status")
    )

    assert out["operation"] == "rollout_status"
    assert out["payload"]["status"] == "failed"
    assert out["payload"]["unavailable_replicas"] == 2


def test_live_k8s_backend_maps_statefulset_api_error(monkeypatch) -> None:
    from packages.tools.k8s import LiveK8sBackend

    class _ApiError(Exception):
        status = 500

    class _AppsV1Api:
        def read_namespaced_stateful_set(
            self,
            name: str,
            namespace: str,
            _request_timeout: float | None = None,
        ):
            raise _ApiError()

    fake_kubernetes = types.ModuleType("kubernetes")
    fake_kubernetes.client = types.SimpleNamespace(  # type: ignore[attr-defined]
        CoreV1Api=lambda: object(),
        AppsV1Api=lambda: _AppsV1Api(),
    )
    fake_kubernetes.config = types.SimpleNamespace(  # type: ignore[attr-defined]
        load_incluster_config=lambda: None,
        load_kube_config=lambda: None,
    )
    monkeypatch.setitem(sys.modules, "kubernetes", fake_kubernetes)

    out = LiveK8sBackend(namespace="data").fetch(
        K8sQuery(service="postgres", operation="get_statefulset")
    )

    assert out == {
        "operation": "get_statefulset",
        "payload": {"error": "api_error", "status_code": 500},
    }


def test_live_k8s_backend_get_statefulset_maps_status(monkeypatch) -> None:
    from packages.tools.k8s import LiveK8sBackend

    class _AppsV1Api:
        def read_namespaced_stateful_set(
            self,
            name: str,
            namespace: str,
            _request_timeout: float | None = None,
        ):
            return types.SimpleNamespace(
                spec=types.SimpleNamespace(
                    replicas=3,
                    template=types.SimpleNamespace(
                        spec=types.SimpleNamespace(
                            containers=[types.SimpleNamespace(image="postgres:16")]
                        )
                    ),
                ),
                status=types.SimpleNamespace(
                    ready_replicas=3,
                    current_replicas=3,
                    updated_replicas=3,
                    current_revision="postgres-8",
                    update_revision="postgres-8",
                    conditions=[types.SimpleNamespace(type="Ready", status="True")],
                ),
            )

    fake_kubernetes = types.ModuleType("kubernetes")
    fake_kubernetes.client = types.SimpleNamespace(  # type: ignore[attr-defined]
        CoreV1Api=lambda: object(),
        AppsV1Api=lambda: _AppsV1Api(),
    )
    fake_kubernetes.config = types.SimpleNamespace(  # type: ignore[attr-defined]
        load_incluster_config=lambda: None,
        load_kube_config=lambda: None,
    )
    monkeypatch.setitem(sys.modules, "kubernetes", fake_kubernetes)

    out = LiveK8sBackend(namespace="data").fetch(
        K8sQuery(service="postgres", operation="get_statefulset")
    )

    assert out["operation"] == "get_statefulset"
    assert out["payload"]["kind"] == "StatefulSet"
    assert out["payload"]["namespace"] == "data"
    assert out["payload"]["replicas"] == 3
    assert out["payload"]["desired_replicas"] == 3
    assert out["payload"]["ready_replicas"] == 3
    assert out["payload"]["updated_replicas"] == 3
    assert out["payload"]["current_revision"] == "postgres-8"
    assert out["payload"]["update_revision"] == "postgres-8"
    assert out["payload"]["image"] == "postgres:16"
    assert out["payload"]["status"] == "complete"


def test_live_k8s_backend_get_statefulset_maps_revision_progressing(monkeypatch) -> None:
    from packages.tools.k8s import LiveK8sBackend

    class _AppsV1Api:
        def read_namespaced_stateful_set(
            self,
            name: str,
            namespace: str,
            _request_timeout: float | None = None,
        ):
            return types.SimpleNamespace(
                metadata=types.SimpleNamespace(generation=8),
                spec=types.SimpleNamespace(
                    replicas=3,
                    template=types.SimpleNamespace(
                        spec=types.SimpleNamespace(
                            containers=[types.SimpleNamespace(image="postgres:16")]
                        )
                    ),
                ),
                status=types.SimpleNamespace(
                    ready_replicas=3,
                    current_replicas=3,
                    updated_replicas=3,
                    current_revision="postgres-7",
                    update_revision="postgres-8",
                    observed_generation=8,
                    conditions=[types.SimpleNamespace(type="Ready", status="True")],
                ),
            )

    fake_kubernetes = types.ModuleType("kubernetes")
    fake_kubernetes.client = types.SimpleNamespace(  # type: ignore[attr-defined]
        CoreV1Api=lambda: object(),
        AppsV1Api=lambda: _AppsV1Api(),
    )
    fake_kubernetes.config = types.SimpleNamespace(  # type: ignore[attr-defined]
        load_incluster_config=lambda: None,
        load_kube_config=lambda: None,
    )
    monkeypatch.setitem(sys.modules, "kubernetes", fake_kubernetes)

    out = LiveK8sBackend(namespace="data").fetch(
        K8sQuery(service="postgres", operation="get_statefulset")
    )

    assert out["operation"] == "get_statefulset"
    assert out["payload"]["namespace"] == "data"
    assert out["payload"]["ready_replicas"] == 3
    assert out["payload"]["updated_replicas"] == 3
    assert out["payload"]["current_revision"] == "postgres-7"
    assert out["payload"]["update_revision"] == "postgres-8"
    assert out["payload"]["status"] == "progressing"


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
