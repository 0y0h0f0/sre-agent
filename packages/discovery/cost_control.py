"""DiscoveryCostController — centralized cost enforcement for discovery operations.

M3 PR 3.3: Enforces limits on Prometheus queries, K8s API calls, and result
sizes. Produces truncation warnings when limits are hit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from packages.discovery.models import DiscoveryCostControl


@dataclass
class CostReport:
    """Summary of cost enforcement during a discovery run."""

    total_queries: int = 0
    truncated_queries: int = 0
    total_bytes_approx: int = 0
    api_calls: int = 0
    warnings: list[str] = field(default_factory=list)


class DiscoveryCostController:
    """Centralized cost enforcement for discovery operations.

    Wraps a DiscoveryCostControl configuration and tracks usage
    during a discovery run. Produces truncation warnings when
    limits are exceeded.

    Usage::

        ctrl = DiscoveryCostController(DiscoveryCostControl(max_metrics=1000))
        metrics = prom_client.list_metrics()
        metrics, truncated = ctrl.limit_metrics(metrics)
        # if truncated: ctrl.report.warnings has the truncation message.
    """

    def __init__(self, config: DiscoveryCostControl | None = None) -> None:
        self._config = config or DiscoveryCostControl()
        self._report = CostReport()

    @property
    def report(self) -> CostReport:
        return self._report

    # ------------------------------------------------------------------
    # Metric name truncation
    # ------------------------------------------------------------------

    def limit_metrics(self, metric_names: list[str]) -> tuple[list[str], bool]:
        """Truncate metric name list to max_metrics, with warning."""
        limit = self._config.max_metrics
        if len(metric_names) > limit:
            self._report.truncated_queries += 1
            self._report.warnings.append(
                f"Metric name list truncated: {len(metric_names)} → {limit} "
                f"(limit={limit})"
            )
            return metric_names[:limit], True
        self._report.total_queries += 1
        return metric_names, False

    # ------------------------------------------------------------------
    # Series limit enforcement
    # ------------------------------------------------------------------

    def check_series_limit(self, series_count: int) -> bool:
        """Check if series count exceeds the per-query limit.

        Returns True if the query should be rejected (too many series).
        """
        limit = self._config.max_series_per_query
        if series_count > limit:
            self._report.truncated_queries += 1
            self._report.warnings.append(
                f"Series query rejected: {series_count} series exceeds "
                f"limit of {limit}"
            )
            return True
        return False

    # ------------------------------------------------------------------
    # Label values truncation
    # ------------------------------------------------------------------

    def limit_label_values(self, values: list[str]) -> tuple[list[str], bool]:
        """Truncate label value list to max_label_values."""
        limit = self._config.max_label_values
        if len(values) > limit:
            self._report.truncated_queries += 1
            self._report.warnings.append(
                f"Label values truncated: {len(values)} → {limit}"
            )
            return values[:limit], True
        return values, False

    # ------------------------------------------------------------------
    # Pod sampling
    # ------------------------------------------------------------------

    def should_sample_pods(self, pod_count: int) -> tuple[bool, int]:
        """Determine whether to sample pods and how many to fetch.

        Returns:
            (should_sample, sample_count)
        """
        ratio = self._config.pod_sample_ratio
        if ratio >= 1.0:
            return False, pod_count
        sample_count = max(1, int(pod_count * ratio))
        if sample_count < pod_count:
            return True, sample_count
        return False, pod_count

    def limit_pods(self, pods: list[Any]) -> tuple[list[Any], bool]:
        """Apply pod sample ratio to a pod list."""
        limit = self._config.max_pods
        if len(pods) > limit:
            self._report.warnings.append(
                f"Pod list truncated: {len(pods)} → {limit}"
            )
            pods = pods[:limit]
            return pods, True
        should_sample, count = self.should_sample_pods(len(pods))
        if should_sample:
            self._report.warnings.append(
                f"Pod sampling applied: {len(pods)} → {count} "
                f"(ratio={self._config.pod_sample_ratio})"
            )
            return pods[:count], True
        return pods, False

    # ------------------------------------------------------------------
    # API call tracking
    # ------------------------------------------------------------------

    def record_api_call(self, bytes_approx: int = 0) -> None:
        """Record an API call for cost tracking."""
        self._report.api_calls += 1
        self._report.total_bytes_approx += bytes_approx
