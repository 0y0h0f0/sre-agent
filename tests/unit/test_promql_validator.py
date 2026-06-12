"""Tests for M1 PR 1.6: PromQL Validator."""
from __future__ import annotations

from unittest.mock import MagicMock

from packages.discovery.models import MetricMapping
from packages.discovery.promql_validator import PromQLValidator


class TestPromQLValidator:
    def test_current_window_has_data_ok(self):
        mapping = MetricMapping(
            semantic_type="latency", metric_name="test_metric",
            promql_template='{metric}{service="svc"}', status="available",
        )
        client = MagicMock()
        client.query_range.return_value = {
            "data": {"result": [{"values": [[0, "1"]]}]},
        }
        validator = PromQLValidator(client)
        result = validator.validate(mapping, "service", "svc")
        assert result.status in ("available", "degraded", "unavailable")

    def test_none_promql_returns_unavailable(self):
        mapping = MetricMapping(
            semantic_type="error_rate", metric_name="",
            promql_template="", status="available",
        )
        client = MagicMock()
        validator = PromQLValidator(client)
        result = validator.validate(mapping)
        assert result.status == "unavailable"
        assert result.confidence == 0.0

    def test_no_data_any_window_no_series(self):
        mapping = MetricMapping(
            semantic_type="qps", metric_name="test_metric",
            promql_template='{metric}{service="svc"}', status="available",
        )
        client = MagicMock()
        client.query_range.return_value = {"data": {"result": []}}
        validator = PromQLValidator(client)
        result = validator.validate(mapping, "service", "svc")
        assert result.status == "unavailable"

    def test_all_windows_empty_but_series_exist(self):
        mapping = MetricMapping(
            semantic_type="latency", metric_name="test_metric",
            promql_template='{metric}{service="svc"}', status="available",
        )
        # One series with no values (empty data).
        client = MagicMock()
        client.query_range.return_value = {
            "data": {"result": [{"metric": {}, "values": []}]},
        }
        validator = PromQLValidator(client)
        result = validator.validate(mapping, "service", "svc")
        # Series exists (total_series > 0) so it's degraded, not unavailable.
        assert result.status == "degraded"

    def test_too_many_series_degraded(self):
        mapping = MetricMapping(
            semantic_type="cpu_throttle", metric_name="test_metric",
            promql_template='{metric}{service="svc"}', status="available",
        )
        client = MagicMock()
        client.query_range.return_value = {
            "data": {"result": [{"values": []} for _ in range(1001)]},
        }
        validator = PromQLValidator(client, max_series=1000)
        result = validator.validate(mapping, "service", "svc")
        assert result.status == "degraded"
        assert "Too many series" in (result.degraded_reason or "")
