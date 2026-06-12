"""Tests for M1 PR 1.3: MetricMatcher."""
from __future__ import annotations

from unittest.mock import MagicMock

from packages.discovery.metric_matcher import MetricMatcher
from packages.discovery.prom_discovery import PrometheusClient


def _mock_client(series_data=None, metadata=None):
    mock = MagicMock(spec=PrometheusClient)
    mock.list_series.return_value = series_data or []
    mock.get_metadata.return_value = metadata or {}
    return mock


class TestMetricMatcher:
    def test_match_latency_histogram(self):
        client = _mock_client(
            series_data=[{"le": "0.1", "service": "checkout"}],
            metadata={"type": "histogram", "unit": "seconds"},
        )
        matcher = MetricMatcher(client)
        results = matcher.match(["http_request_duration_seconds_bucket"])
        assert "latency" in results
        assert results["latency"].status == "available"

    def test_match_error_rate(self):
        client = _mock_client(
            series_data=[{"status": "200", "service": "checkout"}],
            metadata={"type": "counter"},
        )
        matcher = MetricMatcher(client)
        # "http_request_total" matches the regex pattern _(request|http)_(total|count)$
        results = matcher.match(["http_request_total"])
        assert results["error_rate"].status == "available"

    def test_no_candidate_unavailable(self):
        client = _mock_client()
        matcher = MetricMatcher(client)
        results = matcher.match(["completely_unrelated_metric"])
        assert results["latency"].status == "unavailable"
        assert results["error_rate"].status == "unavailable"

    def test_missing_status_label(self):
        # Error rate candidate requires "status" or "code" label.
        client = _mock_client(
            series_data=[{"service": "checkout"}],  # no status/code
            metadata={"type": "counter"},
        )
        matcher = MetricMatcher(client)
        results = matcher.match(["http_requests_total"])
        # Should fall through to next candidate or be unavailable.
        assert results["error_rate"].status in ("unavailable",)

    def test_metadata_type_mismatch_rejected(self):
        # Latency with counter type should be rejected.
        client = _mock_client(
            series_data=[{"le": "0.1"}],
            metadata={"type": "counter"},  # wrong type for latency histogram
        )
        matcher = MetricMatcher(client)
        results = matcher.match(["http_request_duration_seconds_bucket"])
        # Should reject this candidate due to type mismatch.
        assert results["latency"].status != "available"

    def test_gauge_does_not_match_error_rate(self):
        client = _mock_client(
            series_data=[{"service": "checkout"}],
            metadata={"type": "gauge"},  # gauge should not match error_rate
        )
        matcher = MetricMatcher(client)
        results = matcher.match(["some_errors_total"])
        assert results["error_rate"].status != "available"
