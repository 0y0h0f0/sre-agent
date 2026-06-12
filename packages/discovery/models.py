"""Discovery Pydantic models — MetricMapping, MetricCandidate, SEMANTIC_PATTERNS, etc.

Phase 0-8: deterministic discovery models for Prometheus metric matching.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, Field

MetricStatus = Literal["available", "degraded", "unavailable"]
SemanticType = Literal["latency", "error_rate", "qps", "cpu_throttle", "disk_avail"]


class MetricMapping(BaseModel):
    """Result of matching a semantic type to a concrete Prometheus metric."""

    semantic_type: SemanticType
    metric_name: str = ""
    status: MetricStatus
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    promql_template: str = ""
    service_label: str = "service"
    required_labels: list[str] = Field(default_factory=list)
    evidence: dict[str, Any] = Field(default_factory=dict)
    degraded_reason: str | None = None
    alternatives: list[str] = Field(default_factory=list)


class ServiceInfo(BaseModel):
    """Discovered service information."""

    name: str
    namespace: str | None = None
    labels: dict[str, str] = Field(default_factory=dict)
    sources: list[str] = Field(default_factory=list)


class CapabilityMatrix(BaseModel):
    """Diagnostic capabilities available per service."""

    service_name: str
    metrics_available: bool = False
    logs_available: bool = False
    traces_available: bool = False
    k8s_accessible: bool = False
    metric_mappings: list[MetricMapping] = Field(default_factory=list)
    capability_gaps: list[str] = Field(default_factory=list)


class BackendEndpoint(BaseModel):
    """Discovered observability backend endpoint."""

    backend_type: Literal["prometheus", "loki", "jaeger", "alertmanager"]
    url: str
    source: str
    status: Literal["detected_only", "requires_review", "ready", "degraded", "unavailable"]
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    evidence: dict[str, Any] = Field(default_factory=dict)
    auth_required_unknown: bool = True
    degraded_reason: str | None = None


class DiscoveryResult(BaseModel):
    """Top-level discovery output from a DiscoveryRunner run."""

    run_id: str = ""
    services: list[ServiceInfo] = Field(default_factory=list)
    capability_matrix: list[CapabilityMatrix] = Field(default_factory=list)
    metric_mappings: list[MetricMapping] = Field(default_factory=list)
    backend_endpoints: list[BackendEndpoint] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    degraded_signals: list[str] = Field(default_factory=list)
    total_metrics_scanned: int = 0
    total_services_discovered: int = 0
    duration_seconds: float = 0.0
    status: Literal["succeeded", "degraded", "failed"] = "succeeded"


@dataclass
class MetricCandidate:
    """A candidate regex pattern for matching metrics of a semantic type."""

    regex: str
    semantic_type: SemanticType
    priority: int = 0
    required_any_labels: list[str] = field(default_factory=list)
    expected_metric_type: str | None = None
    expected_unit: str | None = None
    promql_template: str = ""
    description: str = ""

    def __post_init__(self) -> None:
        self._compiled = re.compile(self.regex, re.IGNORECASE)

    def matches(self, metric_name: str) -> bool:
        return bool(self._compiled.search(metric_name))

    @property
    def compiled_regex(self) -> re.Pattern[str]:
        return self._compiled


@dataclass
class DiscoveryCostControl:
    """Cost control limits for discovery operations."""

    max_metrics: int = 5000
    max_series_per_query: int = 100
    max_label_values: int = 200
    max_pods: int = 500
    pod_sample_ratio: float = 1.0
    timeout_seconds: float = 10.0
    series_query_timeout_seconds: float = 5.0
    metadata_query_timeout_seconds: float = 3.0
    dry_run_timeout_seconds: float = 8.0
    k8s_list_timeout_seconds: float = 10.0


# ---- SEMANTIC_PATTERNS ----

SEMANTIC_PATTERNS: dict[SemanticType, list[MetricCandidate]] = {
    "latency": [
        MetricCandidate(
            regex=r"_(request|http)_duration_(seconds|milliseconds)_bucket",
            semantic_type="latency",
            priority=0,
            required_any_labels=["le"],
            expected_metric_type="histogram",
            promql_template=(
                "histogram_quantile({quantile}, sum(rate({metric}"
                '{{service_label}=~"{service_name}"}}[{window}])) '
                "by (le, {service_label}))"
            ),
            description="HTTP/gRPC request duration histogram bucket",
        ),
        MetricCandidate(
            regex=r"_(request|http)_duration_(seconds|milliseconds)$",
            semantic_type="latency",
            priority=1,
            required_any_labels=["quantile"],
            expected_metric_type="summary",
            promql_template=(
                "{metric}{quantile=~\"{quantile}\",{service_label}=~\"{service_name}\"}"
            ),
            description="HTTP/gRPC request duration summary",
        ),
        MetricCandidate(
            regex=r"_latency_(seconds|milliseconds)_bucket",
            semantic_type="latency",
            priority=2,
            required_any_labels=["le"],
            expected_metric_type="histogram",
            promql_template=(
                "histogram_quantile({quantile}, sum(rate({metric}"
                '{{service_label}=~"{service_name}"}}[{window}])) '
                "by (le, {service_label}))"
            ),
            description="Generic latency histogram bucket",
        ),
    ],
    "error_rate": [
        MetricCandidate(
            regex=r"_(request|http)_(total|count)$",
            semantic_type="error_rate",
            priority=0,
            required_any_labels=["status", "code"],
            expected_metric_type="counter",
            promql_template=(
                "clamp_min(\n"
                '  sum(rate({metric}{status=~"5..",'
                '{service_label}=~"{service_name}"}[{window}]))\n'
                "  / sum(rate({metric}"
                '{{service_label}=~"{service_name}"}[{window}])),\n'
                "  0\n)"
            ),
            description="HTTP request count with status code label",
        ),
        MetricCandidate(
            regex=r"_errors?_total$",
            semantic_type="error_rate",
            priority=1,
            required_any_labels=[],
            expected_metric_type="counter",
            promql_template=(
                "sum(rate({metric}{{{service_label}=~\"{service_name}\"}}[{window}]))"
            ),
            description="Error counter (no ratio without total)",
        ),
    ],
    "qps": [
        MetricCandidate(
            regex=r"_(request|http)_(total|count)$",
            semantic_type="qps",
            priority=0,
            required_any_labels=[],
            expected_metric_type="counter",
            promql_template=(
                "sum(rate({metric}{{{service_label}=~\"{service_name}\"}}[{window}]))"
                " by ({service_label})"
            ),
            description="HTTP request count as QPS source",
        ),
        MetricCandidate(
            regex=r"_requests_per_second$",
            semantic_type="qps",
            priority=1,
            required_any_labels=[],
            expected_metric_type="gauge",
            promql_template="{metric}{{{service_label}=~\"{service_name}\"}}",
            description="Pre-computed requests per second gauge",
        ),
    ],
    "cpu_throttle": [
        MetricCandidate(
            regex=r"container_cpu_cfs_throttled_seconds_total",
            semantic_type="cpu_throttle",
            priority=0,
            required_any_labels=["container"],
            expected_metric_type="counter",
            promql_template=(
                "rate({metric}{{{service_label}=~\"{service_name}\"}}[{window}])"
            ),
            description="Container CPU CFS throttled seconds",
        ),
    ],
    "disk_avail": [
        MetricCandidate(
            regex=r"node_filesystem_avail_bytes",
            semantic_type="disk_avail",
            priority=0,
            required_any_labels=["mountpoint"],
            expected_metric_type="gauge",
            promql_template=(
                "{metric}{{mountpoint=~\"/|/data\"}}"
            ),
            description="Node filesystem available bytes",
        ),
    ],
}
