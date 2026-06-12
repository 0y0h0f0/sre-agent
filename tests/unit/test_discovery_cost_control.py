"""Tests for M3 PR 3.3: DiscoveryCostController."""

from __future__ import annotations

from packages.discovery.cost_control import DiscoveryCostController
from packages.discovery.models import DiscoveryCostControl


class TestMetricTruncation:
    def test_metric_names_truncated_with_warning(self):
        """Metrics exceeding max_metrics are truncated with a warning."""
        ctrl = DiscoveryCostController(
            DiscoveryCostControl(max_metrics=10)
        )
        names = [f"metric_{i}" for i in range(100)]
        result, truncated = ctrl.limit_metrics(names)

        assert len(result) == 10
        assert truncated is True
        assert len(ctrl.report.warnings) == 1
        assert "truncated" in ctrl.report.warnings[0]

    def test_metrics_within_limit_no_truncation(self):
        """Metrics within limit are not truncated."""
        ctrl = DiscoveryCostController(
            DiscoveryCostControl(max_metrics=5000)
        )
        names = [f"metric_{i}" for i in range(100)]
        result, truncated = ctrl.limit_metrics(names)

        assert len(result) == 100
        assert truncated is False
        assert ctrl.report.warnings == []


class TestSeriesLimit:
    def test_series_over_limit_rejected(self):
        """Series query exceeding max_series_per_query is rejected."""
        ctrl = DiscoveryCostController(
            DiscoveryCostControl(max_series_per_query=50)
        )
        rejected = ctrl.check_series_limit(200)

        assert rejected is True
        assert len(ctrl.report.warnings) == 1
        assert "200 series exceeds" in ctrl.report.warnings[0]

    def test_series_within_limit_accepted(self):
        """Series within limit is accepted."""
        ctrl = DiscoveryCostController(
            DiscoveryCostControl(max_series_per_query=100)
        )
        rejected = ctrl.check_series_limit(50)

        assert rejected is False


class TestPodSampling:
    def test_pod_sample_ratio(self):
        """Pods are sampled at the configured ratio."""
        ctrl = DiscoveryCostController(
            DiscoveryCostControl(pod_sample_ratio=0.5)
        )
        pods = [{"name": f"pod_{i}"} for i in range(100)]
        result, sampled = ctrl.limit_pods(pods)

        assert len(result) == 50
        assert sampled is True
        assert "sampling" in ctrl.report.warnings[0].lower()

    def test_pod_sample_ratio_1_no_sampling(self):
        """Ratio of 1.0 means no sampling."""
        ctrl = DiscoveryCostController(
            DiscoveryCostControl(pod_sample_ratio=1.0)
        )
        pods = [{"name": f"pod_{i}"} for i in range(100)]
        result, sampled = ctrl.limit_pods(pods)

        assert len(result) == 100
        assert sampled is False

    def test_pod_max_limit(self):
        """Pods exceeding max_pods are truncated first."""
        ctrl = DiscoveryCostController(
            DiscoveryCostControl(max_pods=10, pod_sample_ratio=1.0)
        )
        pods = [{"name": f"pod_{i}"} for i in range(100)]
        result, sampled = ctrl.limit_pods(pods)

        assert len(result) == 10
        assert sampled is True


class TestLabelValues:
    def test_label_values_truncated(self):
        """Label values exceeding max_label_values are truncated."""
        ctrl = DiscoveryCostController(
            DiscoveryCostControl(max_label_values=20)
        )
        values = [f"val_{i}" for i in range(100)]
        result, truncated = ctrl.limit_label_values(values)

        assert len(result) == 20
        assert truncated is True


class TestApiCallTracking:
    def test_record_api_call(self):
        """API calls are tracked."""
        ctrl = DiscoveryCostController()
        ctrl.record_api_call(bytes_approx=1024)
        ctrl.record_api_call(bytes_approx=2048)

        assert ctrl.report.api_calls == 2
        assert ctrl.report.total_bytes_approx == 3072


class TestDefaultConfig:
    def test_default_config_works(self):
        """Default DiscoveryCostControl works without arguments."""
        ctrl = DiscoveryCostController()
        names = [f"m_{i}" for i in range(100)]
        result, truncated = ctrl.limit_metrics(names)

        assert len(result) == 100
        assert truncated is False  # default max_metrics is 5000
