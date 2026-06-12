"""Tests for M1 PR 1.4: Prometheus Service Label Detection."""
from __future__ import annotations

from unittest.mock import MagicMock

from packages.discovery.prom_discovery import (
    PrometheusClient,
    detect_metrics_service_label,
)


def _mock_client(series_by_metric=None):
    """series_by_metric: dict of metric_name -> list of series label dicts."""
    data = series_by_metric or {}
    mock = MagicMock(spec=PrometheusClient)
    mock.list_series.side_effect = lambda match: data.get(
        match.split('"')[1] if '"' in match else "", []
    )
    return mock


class TestDetectMetricsServiceLabel:
    def test_service_label_detected(self):
        series_data = {
            "metric_a": [{"service": "checkout", "namespace": "prod"}],
            "metric_b": [{"service": "payments", "namespace": "prod"}],
        }
        client = _mock_client(series_data)
        label, coverage, scores = detect_metrics_service_label(
            client, ["metric_a", "metric_b"], coverage_threshold=0.5,
        )
        assert label == "service"
        assert coverage >= 0.5

    def test_coverage_below_threshold(self):
        series_data = {
            "metric_a": [{"service": "checkout"}],
            "metric_b": [{"app": "payments"}],  # no service label
        }
        client = _mock_client(series_data)
        label, coverage, scores = detect_metrics_service_label(
            client, ["metric_a", "metric_b"], coverage_threshold=0.9,
        )
        assert label is None

    def test_multiple_candidates_highest_wins(self):
        series_data = {
            "metric_a": [{"app": "checkout", "service": "checkout"}],
            "metric_b": [{"app": "payments"}],
        }
        client = _mock_client(series_data)
        label, coverage, scores = detect_metrics_service_label(
            client, ["metric_a", "metric_b"], coverage_threshold=0.5,
        )
        # "app" should win (2/2 vs 1/2 for service)
        assert label == "app"
        assert coverage == 1.0

    def test_no_label_meets_threshold(self):
        series_data = {
            "metric_a": [{"unknown": "x"}],
            "metric_b": [{"other": "y"}],
        }
        client = _mock_client(series_data)
        label, coverage, scores = detect_metrics_service_label(
            client, ["metric_a", "metric_b"], coverage_threshold=0.8,
        )
        assert label is None

    def test_empty_metrics(self):
        client = _mock_client({})
        label, coverage, scores = detect_metrics_service_label(client, [])
        assert label is None
        assert coverage == 0.0
