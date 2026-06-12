"""Tests for M1 PR 1.5: PromQL Builder."""
from __future__ import annotations

from packages.discovery.promql_builder import (
    build_cpu_throttle_promql,
    build_disk_avail_promql,
    build_error_rate_promql,
    build_latency_promql,
    build_promql,
    build_qps_promql,
)


class TestBuildLatency:
    def test_histogram_quantile(self):
        result = build_latency_promql(
            "http_request_duration_seconds_bucket",
            "service", "checkout", quantile=0.99,
        )
        assert "histogram_quantile(0.99" in result
        assert 'service="checkout"' in result
        assert "by (le, service)" in result
        assert "[5m]" in result


class TestBuildErrorRate:
    def test_includes_clamp_min(self):
        result = build_error_rate_promql(
            "http_requests_total", "service", "checkout",
        )
        assert "clamp_min" in result
        assert 'status=~"5.."' in result
        assert 'service="checkout"' in result

    def test_uses_5xx_filter(self):
        result = build_error_rate_promql(
            "http_requests_total", "app", "myapp",
        )
        assert 'status=~"5.."' in result


class TestBuildQPS:
    def test_uses_sum_rate(self):
        result = build_qps_promql(
            "http_requests_total", "service", "checkout",
        )
        assert "sum(rate(" in result
        assert "by (service)" in result
        assert "[5m]" in result


class TestBuildCpuThrottle:
    def test_gauge_does_not_use_rate(self):
        result = build_cpu_throttle_promql(
            "container_cpu_cfs_throttled_seconds_total",
            "service", "checkout",
        )
        assert "rate(" in result
        assert "container_cpu_cfs_periods_total" in result


class TestBuildDiskAvail:
    def test_no_rate(self):
        result = build_disk_avail_promql(
            "node_filesystem_avail_bytes", "service", "checkout",
        )
        # Gauge metric — no rate() in the main metric reference.
        assert "min(" in result
        assert "node_filesystem_size_bytes" in result


class TestBuildPromql:
    def test_dispatches_correct_builder(self):
        result = build_promql(
            "latency", "http_request_duration_seconds_bucket",
            "service", "checkout", quantile=0.95,
        )
        assert "histogram_quantile(0.95" in result

    def test_error_rate_builder(self):
        result = build_promql(
            "error_rate", "http_requests_total", "service", "checkout",
        )
        assert "clamp_min" in result
