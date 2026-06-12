"""Discovery API router — read endpoints for discovery status and results.

M5 PR 5.1: GET /api/discovery/status, /services, /metrics, /topology, /capabilities.
All read endpoints require ``discovery:read`` scope (or ``discovery:write``).

Data is reconstructed from DiscoveryRun summaries, DiscoveryProposals, and
published EffectiveConfig snapshots stored in the database.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from apps.api.dependencies import get_db, require_scope
from apps.api.schemas.discovery import (
    CapabilityMatrixResponse,
    CapabilityResponse,
    DiscoveryRerunRequest,
    DiscoveryRerunResponse,
    DiscoveryRunSummary,
    DiscoveryStatusResponse,
    MetricListResponse,
    MetricMappingResponse,
    ServiceEdgeResponse,
    ServiceListResponse,
    ServiceResponse,
    TopologyResponse,
    WorkloadBindingResponse,
)
from packages.db.repositories.discovery_runs import DiscoveryRunRepository
from packages.db.repositories.effective_configs import EffectiveConfigRepository

router = APIRouter(prefix="/api/discovery", tags=["discovery"])

# Require discovery:read for all endpoints in this router.
_require_read = require_scope("discovery:read", "discovery:write")


def _run_to_summary(run: Any) -> DiscoveryRunSummary:
    """Convert a DiscoveryRun ORM object to a DiscoveryRunSummary."""
    summary = run.summary or {}
    return DiscoveryRunSummary(
        discovery_run_id=run.discovery_run_id,
        source=run.source,
        status=run.status,
        trigger_type=getattr(run, "trigger_type", "automatic"),
        triggered_by=getattr(run, "triggered_by", None),
        started_at=getattr(run, "started_at", None),
        finished_at=getattr(run, "finished_at", None),
        error_message=getattr(run, "error_message", None),
        total_services_discovered=summary.get("total_services_discovered", 0),
        total_metrics_scanned=summary.get("total_metrics_scanned", 0),
        duration_seconds=summary.get("duration_seconds"),
        warnings=summary.get("warnings", []),
        created_at=getattr(run, "created_at", None),
    )


@router.get(
    "/status",
    response_model=DiscoveryStatusResponse,
    dependencies=[Depends(_require_read)],
)
def get_discovery_status(
    db: Session = Depends(get_db),
    limit: int = Query(default=5, ge=1, le=50),
) -> DiscoveryStatusResponse:
    """Return discovery system status and recent run history."""
    from packages.common.settings import get_settings

    settings = get_settings()

    run_repo = DiscoveryRunRepository(db)
    recent_runs = run_repo.list_recent(limit=limit)
    runs_list = list(recent_runs)

    latest = runs_list[0] if runs_list else None

    return DiscoveryStatusResponse(
        discovery_enabled=settings.discovery_enabled,
        latest_run=_run_to_summary(latest) if latest else None,
        recent_runs=[_run_to_summary(r) for r in runs_list],
        total_runs=len(runs_list),
    )


@router.get(
    "/services",
    response_model=ServiceListResponse,
    dependencies=[Depends(_require_read)],
)
def get_discovery_services(
    db: Session = Depends(get_db),
) -> ServiceListResponse:
    """Return services discovered in the latest successful discovery run."""
    run_repo = DiscoveryRunRepository(db)
    recent = run_repo.list_recent(limit=20)

    # Find the most recent successful or degraded run with service data.
    services: list[ServiceResponse] = []
    for run in recent:
        summary = run.summary or {}
        raw_services: list[dict[str, Any]] = summary.get("services", [])
        if raw_services:
            for svc in raw_services:
                services.append(
                    ServiceResponse(
                        name=svc.get("name", "unknown"),
                        namespace=svc.get("namespace"),
                        labels=svc.get("labels", {}),
                        sources=svc.get("sources", []),
                    )
                )
            break  # Use data from the first run that has services.

    return ServiceListResponse(
        services=services,
        total=len(services),
    )


@router.get(
    "/metrics",
    response_model=MetricListResponse,
    dependencies=[Depends(_require_read)],
)
def get_discovery_metrics(
    db: Session = Depends(get_db),
) -> MetricListResponse:
    """Return metric mappings discovered in the latest discovery run."""
    run_repo = DiscoveryRunRepository(db)
    recent = run_repo.list_recent(limit=20)

    metrics: list[MetricMappingResponse] = []
    for run in recent:
        summary = run.summary or {}
        raw_metrics: list[dict[str, Any]] = summary.get("metric_mappings", [])
        if raw_metrics:
            for m in raw_metrics:
                metrics.append(
                    MetricMappingResponse(
                        semantic_type=m.get("semantic_type", "unknown"),
                        metric_name=m.get("metric_name", ""),
                        status=m.get("status", "unavailable"),
                        confidence=m.get("confidence", 0.0),
                        promql_template=m.get("promql_template", ""),
                        service_label=m.get("service_label", "service"),
                        required_labels=m.get("required_labels", []),
                        degraded_reason=m.get("degraded_reason"),
                        alternatives=m.get("alternatives", []),
                    )
                )
            break

    return MetricListResponse(
        metrics=metrics,
        total=len(metrics),
    )


@router.get(
    "/topology",
    response_model=TopologyResponse,
    dependencies=[Depends(_require_read)],
)
def get_discovery_topology(
    db: Session = Depends(get_db),
) -> TopologyResponse:
    """Return service topology (bindings + edges) from latest discovery."""
    run_repo = DiscoveryRunRepository(db)
    recent = run_repo.list_recent(limit=20)

    bindings: list[WorkloadBindingResponse] = []
    edges: list[ServiceEdgeResponse] = []

    for run in recent:
        summary = run.summary or {}
        raw_bindings: list[dict[str, Any]] = summary.get("workload_bindings", [])
        raw_edges: list[dict[str, Any]] = summary.get("service_edges", [])

        if raw_bindings or raw_edges:
            for wb in raw_bindings:
                bindings.append(
                    WorkloadBindingResponse(
                        service_name=wb.get("service_name", "unknown"),
                        workload_name=wb.get("workload_name", "unknown"),
                        workload_kind=wb.get("workload_kind", ""),
                        namespace=wb.get("namespace"),
                    )
                )
            for se in raw_edges:
                edges.append(
                    ServiceEdgeResponse(
                        source_service=se.get("source_service", "unknown"),
                        target_service=se.get("target_service", "unknown"),
                        edge_type=se.get("edge_type", "unknown"),
                        confidence=se.get("confidence", 0.0),
                        evidence=se.get("evidence", {}),
                    )
                )
            break

    return TopologyResponse(
        workload_bindings=bindings,
        service_edges=edges,
    )


@router.get(
    "/capabilities",
    response_model=CapabilityMatrixResponse,
    dependencies=[Depends(_require_read)],
)
def get_discovery_capabilities(
    db: Session = Depends(get_db),
) -> CapabilityMatrixResponse:
    """Return capability matrix for discovered services."""
    run_repo = DiscoveryRunRepository(db)
    recent = run_repo.list_recent(limit=20)

    capabilities: list[CapabilityResponse] = []
    for run in recent:
        summary = run.summary or {}
        raw_caps: list[dict[str, Any]] = summary.get("capability_matrix", [])

        # Also check the published config snapshot for capability data.
        if not raw_caps:
            ec_repo = EffectiveConfigRepository(db)
            latest_config = ec_repo.get_latest_published()
            if latest_config and latest_config.config_snapshot:
                raw_caps = latest_config.config_snapshot.get("capabilities", [])

        if raw_caps:
            for cap in raw_caps:
                metric_mappings = [
                    MetricMappingResponse(
                        semantic_type=m.get("semantic_type", "unknown"),
                        metric_name=m.get("metric_name", ""),
                        status=m.get("status", "unavailable"),
                        confidence=m.get("confidence", 0.0),
                        promql_template=m.get("promql_template", ""),
                        service_label=m.get("service_label", "service"),
                        required_labels=m.get("required_labels", []),
                        degraded_reason=m.get("degraded_reason"),
                        alternatives=m.get("alternatives", []),
                    )
                    for m in cap.get("metric_mappings", [])
                ]
                capabilities.append(
                    CapabilityResponse(
                        service_name=cap.get("service_name", "unknown"),
                        metrics_available=cap.get("metrics_available", False),
                        logs_available=cap.get("logs_available", False),
                        traces_available=cap.get("traces_available", False),
                        k8s_accessible=cap.get("k8s_accessible", False),
                        metric_mappings=metric_mappings,
                        capability_gaps=cap.get("capability_gaps", []),
                    )
                )
            break

    return CapabilityMatrixResponse(
        capabilities=capabilities,
        total_services=len(capabilities),
    )


# ---------------------------------------------------------------------------
# POST /api/discovery/rerun (PR 5.2)
# ---------------------------------------------------------------------------

_require_write = require_scope("discovery:write")


@router.post(
    "/rerun",
    response_model=DiscoveryRerunResponse,
    status_code=202,
    dependencies=[Depends(_require_write)],
)
def trigger_discovery_rerun(
    body: DiscoveryRerunRequest | None = None,
    db: Session = Depends(get_db),
) -> DiscoveryRerunResponse:
    """Trigger a manual discovery rerun.

    Creates a DiscoveryRun record, attempts to acquire the Redis
    discovery lock, and enqueues a Celery task. If another discovery
    run is already in progress, returns status ``"locked"``.

    Requires ``discovery:write`` scope.
    """
    import redis as redis_lib

    from packages.common.redis_lock import RedisLock
    from packages.common.settings import get_settings
    from packages.db.repositories.audit_logs import AuditLogRepository
    from packages.db.repositories.discovery_runs import DiscoveryRunRepository

    triggered_by = body.triggered_by if body else None
    settings = get_settings()

    # Create the DiscoveryRun record.
    run_repo = DiscoveryRunRepository(db)
    run = run_repo.create(
        source="manual_rerun",
        trigger_type="manual",
        triggered_by=triggered_by,
    )
    db.flush()

    # Try to acquire Redis lock.
    r = None
    lock = None
    try:
        r = redis_lib.Redis.from_url(settings.redis_url)
        r.ping()
    except Exception:
        r = None

    if r is not None:
        lock = RedisLock(r, "discovery:runner", ttl=300)
        acquired = lock.acquire()
        if not acquired:
            return DiscoveryRerunResponse(
                discovery_run_id=run.discovery_run_id,
                task_id="",
                status="locked",
                message="Another discovery run is in progress. Try again later.",
            )

    # Enqueue Celery task.
    try:
        from apps.worker.tasks import enqueue_discovery_rerun_task
        task_id = enqueue_discovery_rerun_task(
            run.discovery_run_id, triggered_by=triggered_by
        )
    except Exception as exc:
        if lock is not None:
            try:
                lock.release()
            except Exception:
                pass
        from fastapi import HTTPException
        raise HTTPException(
            status_code=500,
            detail=f"Failed to enqueue discovery task: {exc}",
        ) from exc

    # Audit.
    audit_repo = AuditLogRepository(db)
    audit_repo.create_discovery_audit(
        action="discovery.rerun_requested",
        resource_type="discovery_run",
        resource_id=run.discovery_run_id,
        actor=triggered_by or "system",
        details={
            "trigger_type": "manual",
            "task_id": task_id,
        },
    )
    db.commit()

    return DiscoveryRerunResponse(
        discovery_run_id=run.discovery_run_id,
        task_id=task_id,
        status="enqueued",
        message="Discovery rerun has been enqueued.",
    )
