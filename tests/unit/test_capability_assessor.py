"""Tests for M3 PR 3.2: CapabilityAssessor."""

from __future__ import annotations

from packages.discovery.capability_assessor import CapabilityAssessor
from packages.discovery.models import (
    CapabilityMatrix,
    DiscoveryResult,
    MetricMapping,
)


def _make_result(
    *,
    metric_mappings=None,
    capability_matrix=None,
    degraded_signals=None,
    warnings=None,
    status="succeeded",
) -> DiscoveryResult:
    """Helper to build a DiscoveryResult for assessment."""
    return DiscoveryResult(
        run_id="dr_test",
        services=[],
        capability_matrix=capability_matrix or [],
        metric_mappings=metric_mappings or [],
        backend_endpoints=[],
        warnings=warnings or [],
        degraded_signals=degraded_signals or [],
        total_metrics_scanned=0,
        total_services_discovered=0,
        duration_seconds=1.0,
        status=status,
    )


class TestHealthyResult:
    def test_healthy_all_available(self):
        """Healthy result with all metrics available."""
        result = _make_result(
            metric_mappings=[
                MetricMapping(
                    semantic_type="latency",
                    metric_name="http_request_duration_seconds_bucket",
                    status="available",
                    confidence=0.95,
                ),
                MetricMapping(
                    semantic_type="error_rate",
                    metric_name="http_request_total",
                    status="available",
                    confidence=0.90,
                ),
                MetricMapping(
                    semantic_type="qps",
                    metric_name="http_request_total",
                    status="available",
                    confidence=0.90,
                ),
                MetricMapping(
                    semantic_type="cpu_throttle",
                    metric_name="container_cpu_cfs_throttled_seconds_total",
                    status="available",
                    confidence=0.85,
                ),
                MetricMapping(
                    semantic_type="disk_avail",
                    metric_name="node_filesystem_avail_bytes",
                    status="available",
                    confidence=0.80,
                ),
            ],
        )
        assessor = CapabilityAssessor()
        report = assessor.assess(result)

        assert report.overall == "healthy"
        assert report.capability_gaps == []
        assert report.confidence_adjustment == 1.0


class TestDegradedResult:
    def test_missing_core_metric_is_critical(self):
        """Missing a core metric type (latency) → critical."""
        result = _make_result(
            metric_mappings=[
                MetricMapping(
                    semantic_type="error_rate",
                    metric_name="http_request_total",
                    status="available",
                    confidence=0.90,
                ),
            ],
        )
        assessor = CapabilityAssessor()
        report = assessor.assess(result)

        assert report.overall == "critical"
        assert "metric_latency_missing" in report.capability_gaps

    def test_missing_extended_metric_is_degraded(self):
        """Missing an extended metric type only → degraded, not critical."""
        result = _make_result(
            metric_mappings=[
                MetricMapping(
                    semantic_type="latency",
                    metric_name="http_request_duration_seconds_bucket",
                    status="available",
                    confidence=0.95,
                ),
                MetricMapping(
                    semantic_type="error_rate",
                    metric_name="http_request_total",
                    status="available",
                    confidence=0.90,
                ),
                MetricMapping(
                    semantic_type="qps",
                    metric_name="http_request_total",
                    status="available",
                    confidence=0.90,
                ),
            ],
        )
        assessor = CapabilityAssessor()
        report = assessor.assess(result)

        assert report.overall == "degraded"
        assert "metric_cpu_throttle_missing" in report.degraded_signals

    def test_degraded_backend_preserved(self):
        """Degraded signals from the runner are preserved alongside metric gaps."""
        result = _make_result(
            metric_mappings=[
                MetricMapping(
                    semantic_type="latency",
                    metric_name="http_request_duration_seconds_bucket",
                    status="available",
                    confidence=0.95,
                ),
                MetricMapping(
                    semantic_type="error_rate",
                    metric_name="http_request_total",
                    status="available",
                    confidence=0.90,
                ),
                MetricMapping(
                    semantic_type="qps",
                    metric_name="http_request_total",
                    status="available",
                    confidence=0.90,
                ),
            ],
            degraded_signals=["prometheus_unavailable"],
            status="degraded",
        )
        assessor = CapabilityAssessor()
        report = assessor.assess(result)

        assert report.overall == "degraded"
        assert "prometheus_unavailable" in report.degraded_signals

    def test_service_label_fallback_detected(self):
        """Fallback to default service label is detected from warnings."""
        result = _make_result(
            warnings=[
                "Prometheus service label detection: coverage=0%, using default 'service'",
            ],
        )
        assessor = CapabilityAssessor()
        report = assessor.assess(result)

        assert "prometheus_service_label_fallback" in report.used_fallback_signals

    def test_loki_service_label_fallback_detected(self):
        """Loki service label fallback detected."""
        result = _make_result(
            warnings=[
                "Loki service label detection: coverage=30%, using default 'service'",
            ],
        )
        assessor = CapabilityAssessor()
        report = assessor.assess(result)

        assert "loki_service_label_fallback" in report.used_fallback_signals


class TestConfidenceAdjustment:
    def test_no_gaps_no_adjustment(self):
        """All expected metric types available → confidence adjustment is 1.0."""
        result = _make_result(
            metric_mappings=[
                MetricMapping(
                    semantic_type=stype,
                    metric_name=f"test_{stype}_metric",
                    status="available",
                    confidence=0.90,
                )
                for stype in ("latency", "error_rate", "qps", "cpu_throttle", "disk_avail")
            ],
        )
        assessor = CapabilityAssessor()
        report = assessor.assess(result)

        assert report.confidence_adjustment == 1.0

    def test_multiple_gaps_reduce_confidence(self):
        """Multiple gaps reduce confidence significantly."""
        result = _make_result(
            degraded_signals=["prometheus_unavailable", "loki_unavailable"],
            status="degraded",
        )
        assessor = CapabilityAssessor()
        report = assessor.assess(result)

        # Core metrics are missing (no metric mappings) + 2 degraded signals
        assert report.confidence_adjustment < 1.0
        assert report.confidence_adjustment >= 0.30  # floor

    def test_confidence_has_floor(self):
        """Confidence adjustment never drops below 0.30."""
        result = _make_result(
            degraded_signals=[
                "prometheus_unavailable",
                "loki_unavailable",
                "jaeger_unavailable",
                "k8s_unavailable",
                "backend_endpoints_unavailable",
            ],
            status="failed",
        )
        assessor = CapabilityAssessor()
        report = assessor.assess(result)

        assert report.confidence_adjustment >= 0.30


class TestPerServiceAssessment:
    def test_per_service_gaps(self):
        """Per-service assessment captures gaps per service."""
        result = _make_result(
            capability_matrix=[
                CapabilityMatrix(
                    service_name="api",
                    metrics_available=False,
                    logs_available=True,
                    traces_available=False,
                    k8s_accessible=True,
                    capability_gaps=["metrics_unavailable", "traces_unavailable"],
                ),
            ],
        )
        assessor = CapabilityAssessor()
        report = assessor.assess(result)

        assert "api" in report.per_service
        svc = report.per_service["api"]
        assert "metrics_unavailable" in svc["gaps"]
        assert not svc["has_metrics"]


class TestCustomTypes:
    def test_custom_core_types(self):
        """Custom core/extended type sets are respected."""
        result = _make_result(
            metric_mappings=[
                MetricMapping(
                    semantic_type="latency",
                    metric_name="http_request_duration_seconds_bucket",
                    status="available",
                    confidence=0.95,
                ),
            ],
        )
        # Only latency is core; everything else is extended.
        assessor = CapabilityAssessor(
            core_types={"latency"},
            extended_types={"error_rate", "qps", "cpu_throttle", "disk_avail"},
        )
        report = assessor.assess(result)

        # Missing error_rate and qps are extended → degraded, not gaps.
        assert "metric_error_rate_missing" in report.degraded_signals
        assert "metric_latency_missing" not in report.capability_gaps
        # No core gaps → should not be critical.
        assert report.overall == "degraded"
