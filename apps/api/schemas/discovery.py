"""Discovery API schemas — request/response models for discovery endpoints.

M5 PR 5.1: Read-only discovery status, services, metrics, topology, capabilities.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Discovery Status
# ---------------------------------------------------------------------------


class DiscoveryRunSummary(BaseModel):
    """Summary of a single discovery run."""

    discovery_run_id: str
    source: str  # scheduled | manual_rerun | startup
    status: str  # running | succeeded | degraded | failed
    trigger_type: str = "automatic"
    triggered_by: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error_message: str | None = None
    total_services_discovered: int = 0
    total_metrics_scanned: int = 0
    duration_seconds: float | None = None
    warnings: list[str] = Field(default_factory=list)
    created_at: datetime | None = None

    model_config = {"from_attributes": True}


class DiscoveryStatusResponse(BaseModel):
    """Status overview of the discovery system."""

    discovery_enabled: bool
    latest_run: DiscoveryRunSummary | None = None
    recent_runs: list[DiscoveryRunSummary] = Field(default_factory=list)
    total_runs: int = 0


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class ServiceResponse(BaseModel):
    """A discovered service."""

    name: str
    namespace: str | None = None
    labels: dict[str, str] = Field(default_factory=dict)
    sources: list[str] = Field(default_factory=list)


class ServiceListResponse(BaseModel):
    """Paginated list of discovered services."""

    services: list[ServiceResponse] = Field(default_factory=list)
    total: int = 0


# ---------------------------------------------------------------------------
# Metric Mapping
# ---------------------------------------------------------------------------


class MetricMappingResponse(BaseModel):
    """A matched metric mapping."""

    semantic_type: str
    metric_name: str = ""
    status: str  # available | degraded | unavailable
    confidence: float = 0.0
    promql_template: str = ""
    service_label: str = "service"
    required_labels: list[str] = Field(default_factory=list)
    degraded_reason: str | None = None
    alternatives: list[str] = Field(default_factory=list)


class MetricListResponse(BaseModel):
    """List of discovered metric mappings."""

    metrics: list[MetricMappingResponse] = Field(default_factory=list)
    total: int = 0


# ---------------------------------------------------------------------------
# Topology
# ---------------------------------------------------------------------------


class ServiceEdgeResponse(BaseModel):
    """A derived service-to-service edge."""

    source_service: str
    target_service: str
    edge_type: str  # manual | trace | env | configmap
    confidence: float = 0.0
    evidence: dict[str, Any] = Field(default_factory=dict)


class WorkloadBindingResponse(BaseModel):
    """A workload binding (service selector → workload)."""

    service_name: str
    workload_name: str
    workload_kind: str = ""  # Deployment | StatefulSet | DaemonSet
    namespace: str | None = None


class TopologyResponse(BaseModel):
    """Service topology (bindings + edges)."""

    workload_bindings: list[WorkloadBindingResponse] = Field(default_factory=list)
    service_edges: list[ServiceEdgeResponse] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Capability Matrix
# ---------------------------------------------------------------------------


class CapabilityResponse(BaseModel):
    """Per-service diagnostic capability assessment."""

    service_name: str
    metrics_available: bool = False
    logs_available: bool = False
    traces_available: bool = False
    k8s_accessible: bool = False
    metric_mappings: list[MetricMappingResponse] = Field(default_factory=list)
    capability_gaps: list[str] = Field(default_factory=list)


class CapabilityMatrixResponse(BaseModel):
    """Capability matrix for all discovered services."""

    capabilities: list[CapabilityResponse] = Field(default_factory=list)
    total_services: int = 0


# ---------------------------------------------------------------------------
# Discovery Rerun (PR 5.2)
# ---------------------------------------------------------------------------


class DiscoveryRerunRequest(BaseModel):
    """Request body for triggering a discovery rerun."""

    triggered_by: str | None = None


class DiscoveryRerunResponse(BaseModel):
    """Response after enqueuing a discovery rerun."""

    discovery_run_id: str
    task_id: str
    status: str  # "enqueued" | "locked"
    message: str = ""
    locked_by_run_id: str | None = None
