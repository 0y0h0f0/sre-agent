"""Tests for M1 PR 1.1: Discovery Pydantic models."""
from __future__ import annotations

from packages.discovery.models import (
    SEMANTIC_PATTERNS,
    CapabilityMatrix,
    DiscoveryCostControl,
    DiscoveryResult,
    MetricCandidate,
    MetricMapping,
    ServiceInfo,
)


class TestMetricMapping:
    def test_metric_mapping_available(self):
        m = MetricMapping(
            semantic_type="latency",
            metric_name="http_request_duration_seconds_bucket",
            status="available",
            confidence=0.95,
            promql_template="histogram_quantile(0.99, ...)",
        )
        assert m.status == "available"
        assert m.confidence == 0.95

    def test_metric_mapping_degraded(self):
        m = MetricMapping(
            semantic_type="error_rate",
            status="degraded",
            degraded_reason="Missing status label",
            confidence=0.3,
        )
        assert m.status == "degraded"
        assert m.degraded_reason == "Missing status label"

    def test_metric_mapping_unavailable(self):
        m = MetricMapping(
            semantic_type="qps",
            status="unavailable",
            confidence=0.0,
        )
        assert m.status == "unavailable"

    def test_metric_mapping_serialization(self):
        m = MetricMapping(
            semantic_type="latency",
            metric_name="test_metric",
            status="available",
            confidence=0.9,
        )
        data = m.model_dump()
        assert data["semantic_type"] == "latency"
        assert data["metric_name"] == "test_metric"


class TestMetricCandidate:
    def test_regex_compiles(self):
        c = MetricCandidate(regex=r"test_metric", semantic_type="latency")
        assert c.compiled_regex is not None

    def test_matches(self):
        c = MetricCandidate(regex=r"http_request_duration", semantic_type="latency")
        assert c.matches("http_request_duration_seconds_bucket")
        assert not c.matches("unrelated_metric")

    def test_priority_default(self):
        c = MetricCandidate(regex=r"test", semantic_type="latency")
        assert c.priority == 0


class TestSemanticPatterns:
    def test_all_types_present(self):
        assert set(SEMANTIC_PATTERNS.keys()) == {
            "latency", "error_rate", "qps", "cpu_throttle", "disk_avail",
        }

    def test_latency_requires_le(self):
        for c in SEMANTIC_PATTERNS["latency"]:
            assert "le" in c.required_any_labels or c.expected_metric_type

    def test_error_rate_has_status_label(self):
        error_candidates = SEMANTIC_PATTERNS["error_rate"]
        assert any("status" in c.required_any_labels or "code" in c.required_any_labels
                   for c in error_candidates)

    def test_all_have_promql_template(self):
        for stype, candidates in SEMANTIC_PATTERNS.items():
            for c in candidates:
                assert c.promql_template, f"{stype} candidate missing promql_template"

    def test_candidates_have_regex(self):
        for stype, candidates in SEMANTIC_PATTERNS.items():
            for c in candidates:
                assert c.regex, f"{stype} candidate missing regex"


class TestDiscoveryResult:
    def test_serialization(self):
        r = DiscoveryResult(
            run_id="run-1",
            total_metrics_scanned=100,
            total_services_discovered=5,
            status="succeeded",
        )
        data = r.model_dump()
        assert data["run_id"] == "run-1"
        assert data["status"] == "succeeded"

    def test_with_warnings(self):
        r = DiscoveryResult(
            run_id="run-2",
            warnings=["Backend timeout"],
            degraded_signals=["latency degraded"],
            status="degraded",
        )
        assert len(r.warnings) == 1
        assert len(r.degraded_signals) == 1


class TestDiscoveryCostControl:
    def test_defaults(self):
        c = DiscoveryCostControl()
        assert c.max_metrics == 5000
        assert c.timeout_seconds == 10.0

    def test_custom_values(self):
        c = DiscoveryCostControl(max_metrics=100, timeout_seconds=5.0)
        assert c.max_metrics == 100
        assert c.timeout_seconds == 5.0


class TestServiceInfo:
    def test_create(self):
        s = ServiceInfo(name="checkout", namespace="prod", sources=["k8s"])
        assert s.name == "checkout"
        assert s.namespace == "prod"


class TestCapabilityMatrix:
    def test_defaults(self):
        m = CapabilityMatrix(service_name="test")
        assert m.metrics_available is False
        assert m.logs_available is False
