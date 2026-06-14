"""Tests for M3 PR 3.1: DiscoveryRunner orchestration."""

from __future__ import annotations

from unittest.mock import MagicMock

from packages.discovery.backend_endpoints import BackendEndpointDetector, BackendEndpoints
from packages.discovery.k8s_discovery import (
    K8sConfigMap,
    K8sDiscovery,
    K8sDiscoveryResult,
    K8sPod,
    K8sService,
    K8sUnavailableError,
    K8sWorkload,
)
from packages.discovery.loki_discovery import (
    LokiAuthError,
    LokiClient,
    LokiClientError,
)
from packages.discovery.models import DiscoveryResult
from packages.discovery.prom_discovery import (
    PrometheusAuthError,
    PrometheusClient,
    PrometheusClientError,
)
from packages.discovery.runner import DiscoveryRunner

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_prom_client(metric_names=None):
    """Create a PrometheusClient that returns the given metric names."""
    client = MagicMock(spec=PrometheusClient)
    client.list_metrics.return_value = metric_names or [
        "http_request_duration_seconds_bucket",
        "http_request_total",
        "container_cpu_cfs_throttled_seconds_total",
        "node_filesystem_avail_bytes",
        "http_requests_per_second",
    ]
    client.list_series.return_value = [
        {"__name__": "m1", "service": "api", "le": "0.5"},
        {"__name__": "m1", "service": "api", "le": "1.0"},
    ]
    client.get_metadata.return_value = {"type": "histogram", "unit": "seconds"}
    return client


def _mock_loki_client():
    """Create a LokiClient that returns basic labels."""
    client = MagicMock(spec=LokiClient)
    client.list_labels.return_value = ["service", "app", "job", "level"]
    client.list_label_values.return_value = ["api", "gateway", "db"]
    client.query_range.return_value = {
        "data": {"result": [{"stream": {"service": "api"}}]}
    }
    return client


def _mock_k8s_discovery():
    """Create a K8sDiscovery that returns a healthy result."""
    k8s = MagicMock(spec=K8sDiscovery)
    k8s.discover_all.return_value = K8sDiscoveryResult(
        services=[
            K8sService(name="api", namespace="prod", selector={"app": "api"}),
            K8sService(name="gateway", namespace="prod", selector={"app": "gateway"}),
        ],
        workloads=[
            K8sWorkload(
                name="api",
                namespace="prod",
                kind="Deployment",
                config_map_refs=["api-config"],
            ),
            K8sWorkload(name="gateway", namespace="prod", kind="Deployment"),
        ],
        pods=[
            K8sPod(
                name="api-abc",
                namespace="prod",
                labels={"app": "api"},
                owner_references=[{"kind": "Deployment", "name": "api"}],
            ),
        ],
        config_maps=[
            K8sConfigMap(
                name="api-config",
                namespace="prod",
                service_refs=[{"key": "gateway_url", "target_service": "gateway.prod"}],
            ),
        ],
        namespaces=["prod"],
    )
    return k8s


def _mock_jaeger_client():
    """Create a JaegerDiscoveryClient that returns services."""
    from packages.discovery.jaeger_discovery import TraceServiceDiscoveryResult

    client = MagicMock()
    client.discover_services.return_value = TraceServiceDiscoveryResult(
        available_services=["api", "gateway"],
        status="succeeded",
        confidence=0.9,
    )
    return client


def _mock_backend_detector():
    """Create a BackendEndpointDetector."""
    detector = MagicMock(spec=BackendEndpointDetector)
    detector.detect.return_value = []
    return detector


# ---------------------------------------------------------------------------
# Tests: Runner success
# ---------------------------------------------------------------------------

class TestRunnerSuccess:
    def test_runner_success_all_backends(self):
        """Runner succeeds when all backends are healthy."""
        runner = DiscoveryRunner(
            k8s=_mock_k8s_discovery(),
            prom_client=_mock_prom_client(),
            loki_client=_mock_loki_client(),
            jaeger_client=_mock_jaeger_client(),
            backend_detector=_mock_backend_detector(),
        )
        result = runner.run(run_id="dr_test_1")

        assert isinstance(result, DiscoveryResult)
        assert result.run_id == "dr_test_1"
        assert result.status == "succeeded"
        assert result.total_services_discovered >= 2
        assert len(result.metric_mappings) > 0
        assert len(result.workload_bindings) == 1
        assert result.workload_bindings[0].service_name == "api"
        assert len(result.service_edges) == 1
        assert result.service_edges[0].edge_type == "configmap"
        assert result.duration_seconds >= 0

    def test_runner_output_includes_warnings_list(self):
        """Runner output always includes a warnings list."""
        runner = DiscoveryRunner(
            k8s=_mock_k8s_discovery(),
            prom_client=_mock_prom_client(),
        )
        result = runner.run()

        assert isinstance(result.warnings, list)
        # Loki and Jaeger are not configured → warnings expected
        has_loki_warning = any("loki" in w.lower() for w in result.warnings)
        has_jaeger_warning = any("jaeger" in w.lower() for w in result.warnings)
        assert has_loki_warning or has_jaeger_warning

    def test_runner_accepts_real_jaeger_client_list_services(self):
        """Runner works with JaegerDiscoveryClient, not only discover_services mocks."""
        from packages.discovery.jaeger_discovery import JaegerDiscoveryClient

        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {"data": ["api"]}
        response.text = ""
        http_client = MagicMock()
        http_client.get.return_value = response
        runner = DiscoveryRunner(
            k8s=_mock_k8s_discovery(),
            prom_client=_mock_prom_client(),
            jaeger_client=JaegerDiscoveryClient(
                "http://jaeger-query:16686",
                client=http_client,
            ),
        )
        result = runner.run()
        assert any(service.name == "api" for service in result.services)


# ---------------------------------------------------------------------------
# Tests: Degraded Prometheus
# ---------------------------------------------------------------------------

class TestRunnerPrometheusDown:
    def test_runner_prometheus_down_degraded(self):
        """Runner continues when Prometheus is unavailable."""
        prom = MagicMock(spec=PrometheusClient)
        prom.list_metrics.side_effect = PrometheusClientError("connection refused")

        runner = DiscoveryRunner(
            k8s=_mock_k8s_discovery(),
            prom_client=prom,
            loki_client=_mock_loki_client(),
        )
        result = runner.run()

        assert result.status == "degraded"
        assert "prometheus_unavailable" in result.degraded_signals
        assert len(result.metric_mappings) == 0

    def test_runner_prometheus_auth_error(self):
        """Runner continues when Prometheus returns auth error."""
        prom = MagicMock(spec=PrometheusClient)
        prom.list_metrics.side_effect = PrometheusAuthError("401")

        runner = DiscoveryRunner(k8s=_mock_k8s_discovery(), prom_client=prom)
        result = runner.run()

        assert "prometheus_unavailable" in result.degraded_signals


# ---------------------------------------------------------------------------
# Tests: Degraded K8s
# ---------------------------------------------------------------------------

class TestRunnerK8sDown:
    def test_runner_k8s_unavailable_degraded(self):
        """Runner continues when K8s is unavailable."""
        k8s = MagicMock(spec=K8sDiscovery)
        k8s.discover_all.side_effect = K8sUnavailableError("not in cluster")

        runner = DiscoveryRunner(
            k8s=k8s,
            prom_client=_mock_prom_client(),
        )
        result = runner.run()

        assert "k8s_unavailable" in result.degraded_signals
        assert any("not in cluster" in w for w in result.warnings)

    def test_runner_k8s_rbac_forbidden_degraded(self):
        """Runner continues when K8s RBAC is insufficient."""
        k8s = MagicMock(spec=K8sDiscovery)
        k8s.discover_all.side_effect = K8sUnavailableError("RBAC forbidden")

        runner = DiscoveryRunner(k8s=k8s, prom_client=_mock_prom_client())
        result = runner.run()

        assert "k8s_unavailable" in result.degraded_signals


# ---------------------------------------------------------------------------
# Tests: Degraded Loki
# ---------------------------------------------------------------------------

class TestRunnerLokiDown:
    def test_runner_loki_down_degraded(self):
        """Runner continues when Loki is unavailable."""
        loki = MagicMock(spec=LokiClient)
        loki.list_labels.side_effect = LokiClientError("connection refused")

        runner = DiscoveryRunner(
            k8s=_mock_k8s_discovery(),
            prom_client=_mock_prom_client(),
            loki_client=loki,
        )
        result = runner.run()

        assert "loki_unavailable" in result.degraded_signals

    def test_runner_loki_auth_error(self):
        """Runner continues when Loki returns auth error."""
        loki = MagicMock(spec=LokiClient)
        loki.list_labels.side_effect = LokiAuthError("403")

        runner = DiscoveryRunner(
            k8s=_mock_k8s_discovery(),
            prom_client=_mock_prom_client(),
            loki_client=loki,
        )
        result = runner.run()

        assert "loki_unavailable" in result.degraded_signals


# ---------------------------------------------------------------------------
# Tests: Degraded Jaeger
# ---------------------------------------------------------------------------

class TestRunnerJaegerDown:
    def test_runner_jaeger_down_degraded(self):
        """Runner continues when Jaeger is unavailable."""
        jaeger = MagicMock()
        jaeger.discover_services.side_effect = RuntimeError("jaeger unreachable")

        runner = DiscoveryRunner(
            k8s=_mock_k8s_discovery(),
            prom_client=_mock_prom_client(),
            jaeger_client=jaeger,
        )
        result = runner.run()

        assert "jaeger_unavailable" in result.degraded_signals


# ---------------------------------------------------------------------------
# Tests: Degraded Backend Detection
# ---------------------------------------------------------------------------

class TestRunnerBackendDetection:
    def test_runner_backend_endpoint_detection_degraded(self):
        """Runner continues when backend endpoint detection fails."""
        detector = MagicMock(spec=BackendEndpointDetector)
        detector.detect.side_effect = RuntimeError("detection failed")

        runner = DiscoveryRunner(
            k8s=_mock_k8s_discovery(),
            prom_client=_mock_prom_client(),
            backend_detector=detector,
        )
        result = runner.run()

        assert "backend_endpoints_unavailable" in result.degraded_signals

    def test_runner_backend_detection_success(self):
        """Runner includes discovered backend endpoints."""
        detector = MagicMock(spec=BackendEndpointDetector)
        detector.detect.return_value = [
            BackendEndpoints(
                backend_type="prometheus",
                url="http://prometheus.monitoring.svc:9090",
                source="k8s_service",
                status="ready",
                confidence=0.9,
            ),
        ]

        runner = DiscoveryRunner(
            k8s=_mock_k8s_discovery(),
            prom_client=_mock_prom_client(),
            backend_detector=detector,
        )
        result = runner.run()

        assert len(result.backend_endpoints) == 1
        assert result.backend_endpoints[0].backend_type == "prometheus"


# ---------------------------------------------------------------------------
# Tests: Missing semantic types
# ---------------------------------------------------------------------------

class TestMissingSignals:
    def test_missing_latency_metric_adds_capability_gap(self):
        """When latency metric is unavailable, it's reflected in capability gaps."""
        prom = _mock_prom_client(metric_names=["some_other_metric"])
        prom.list_series.return_value = [{"__name__": "some_other_metric"}]

        runner = DiscoveryRunner(
            k8s=_mock_k8s_discovery(),
            prom_client=prom,
        )
        result = runner.run()

        # Missing semantic types appear in capability matrix gaps.
        all_gaps: list[str] = []
        for cap in result.capability_matrix:
            all_gaps.extend(cap.capability_gaps)
        assert "metrics_unavailable" in all_gaps

    def test_runner_output_includes_degraded_signals(self):
        """Degraded signals list captures all partial failures."""
        prom = MagicMock(spec=PrometheusClient)
        prom.list_metrics.side_effect = PrometheusClientError("down")
        loki = MagicMock(spec=LokiClient)
        loki.list_labels.side_effect = LokiClientError("down")

        runner = DiscoveryRunner(
            k8s=_mock_k8s_discovery(),
            prom_client=prom,
            loki_client=loki,
        )
        result = runner.run()

        assert "prometheus_unavailable" in result.degraded_signals
        assert "loki_unavailable" in result.degraded_signals


# ---------------------------------------------------------------------------
# Tests: No components configured
# ---------------------------------------------------------------------------

class TestRunnerNoComponents:
    def test_runner_all_none(self):
        """Runner with no components returns empty result."""
        runner = DiscoveryRunner()
        result = runner.run(run_id="empty")

        assert result.status == "failed"
        assert result.services == []
        assert result.metric_mappings == []

    def test_runner_minimal_with_k8s_only(self):
        """Runner with only K8s returns services but no metrics."""
        runner = DiscoveryRunner(k8s=_mock_k8s_discovery())
        result = runner.run()

        assert result.total_services_discovered >= 2
        assert len(result.metric_mappings) == 0
        # Should have warnings about missing prom/loki/jaeger
        assert len(result.warnings) > 0
