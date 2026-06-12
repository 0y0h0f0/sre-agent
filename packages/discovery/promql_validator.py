"""PromQLValidator — dry-run PromQL validation with multi-window analysis.

M1 PR 1.6: Validates generated PromQL across three time windows ([5m], [1h],
[6h]). Determines status: available (current has data), degraded (series exist
or older windows have data), unavailable (no data, no series).
"""

from __future__ import annotations

import time
from typing import Any

from packages.discovery.models import MetricMapping, MetricStatus
from packages.discovery.prom_discovery import PrometheusClient

_VALIDATION_WINDOWS: list[tuple[str, int]] = [
    ("5m", 300),
    ("1h", 3600),
    ("6h", 21600),
]
_MAX_SERIES_LIMIT = 1000


class PromQLValidator:
    """Validates PromQL by dry-running against Prometheus."""

    def __init__(
        self,
        client: PrometheusClient,
        max_series: int = _MAX_SERIES_LIMIT,
    ) -> None:
        self._client = client
        self._max_series = max_series

    def validate(
        self,
        mapping: MetricMapping,
        service_label: str = "service",
        service_name: str = "",
    ) -> MetricMapping:
        """Dry-run a MetricMapping's PromQL and update status/confidence."""
        if not mapping.promql_template or not mapping.metric_name:
            return mapping.model_copy(
                update={
                    "status": "unavailable",
                    "degraded_reason": "Cannot validate: no PromQL or metric name",
                    "confidence": 0.0,
                }
            )

        promql = mapping.promql_template.replace("{service_label}", service_label)
        promql = promql.replace("{service_name}", service_name)

        window_results: dict[str, bool] = {}
        total_series = 0

        for window_name, window_seconds in _VALIDATION_WINDOWS:
            end = int(time.time())
            start = end - window_seconds
            step = "30s" if window_seconds <= 3600 else "5m"
            try:
                result = self._client.query_range(promql, start, end, step)
            except Exception:
                window_results[window_name] = False
                continue
            data = result.get("data", {})
            series_list = data.get("result", [])
            total_series += len(series_list)
            window_results[window_name] = self._has_values(series_list)

        status, reason, confidence = self._determine_status(
            window_results, total_series, mapping.metric_name
        )
        return mapping.model_copy(
            update={"status": status, "degraded_reason": reason, "confidence": confidence}
        )

    @staticmethod
    def _has_values(series_list: list[dict[str, Any]]) -> bool:
        for series in series_list:
            values = series.get("values", [])
            if values and len(values) > 0:
                return True
        return False

    def _determine_status(
        self,
        window_results: dict[str, bool],
        total_series: int,
        metric_name: str,
    ) -> tuple[MetricStatus, str, float]:
        if total_series > self._max_series:
            return ("degraded", f"Too many series ({total_series} > {self._max_series})", 0.4)
        current = window_results.get("5m", False)
        hour = window_results.get("1h", False)
        six_hour = window_results.get("6h", False)
        if current:
            return ("available", "Current window has data", 0.95)
        if hour:
            return ("degraded", "Current window empty but 1h has data", 0.6)
        if six_hour:
            return ("degraded", "Current and 1h empty but 6h has data", 0.45)
        if total_series > 0:
            return ("degraded", "No data in any window but series exist", 0.3)
        return ("unavailable", "No data in any window and no series exist", 0.0)
