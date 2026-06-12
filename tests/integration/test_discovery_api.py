"""Integration tests for Discovery Read API (PR 5.1).

Tests GET /api/discovery/status, /services, /metrics, /topology, /capabilities.
"""

from __future__ import annotations

from datetime import datetime, timezone

from packages.db.models import DiscoveryRun
from packages.db.repositories.discovery_runs import DiscoveryRunRepository


def _create_discovery_run(
    db,
    *,
    discovery_run_id: str = "dr_test001",
    source: str = "manual_rerun",
    status: str = "succeeded",
    trigger_type: str = "manual",
    triggered_by: str | None = "operator-key-1",
    summary: dict | None = None,
) -> DiscoveryRun:
    """Helper to create a DiscoveryRun record directly in the DB."""
    from packages.common.time import utc_now

    run = DiscoveryRun(
        discovery_run_id=discovery_run_id,
        source=source,
        status=status,
        trigger_type=trigger_type,
        triggered_by=triggered_by,
        started_at=datetime(2026, 6, 12, 10, 0, 0, tzinfo=timezone.utc),
        finished_at=datetime(2026, 6, 12, 10, 0, 5, tzinfo=timezone.utc),
        summary=summary or {},
    )
    db.add(run)
    db.flush()
    return run


# ---------------------------------------------------------------------------
# GET /api/discovery/status
# ---------------------------------------------------------------------------


def test_discovery_status_no_runs(client, db_session):
    """Returns empty status when no discovery runs exist."""
    response = client.get("/api/discovery/status")
    assert response.status_code == 200, response.json()
    data = response.json()
    assert data["discovery_enabled"] is True  # local default
    assert data["latest_run"] is None
    assert data["recent_runs"] == []
    assert data["total_runs"] == 0


def test_discovery_status_with_runs(client, db_session):
    """Returns status with recent run history."""
    _create_discovery_run(
        db_session,
        discovery_run_id="dr_status001",
        source="scheduled",
        status="succeeded",
        trigger_type="automatic",
        summary={
            "total_services_discovered": 5,
            "total_metrics_scanned": 120,
            "duration_seconds": 2.5,
            "warnings": [],
        },
    )
    _create_discovery_run(
        db_session,
        discovery_run_id="dr_status002",
        source="manual_rerun",
        status="degraded",
        trigger_type="manual",
        triggered_by="op-key-1",
        summary={
            "total_services_discovered": 3,
            "total_metrics_scanned": 80,
            "duration_seconds": 1.8,
            "warnings": ["Prometheus unavailable"],
            "degraded_signals": ["prometheus_unavailable"],
        },
    )

    response = client.get("/api/discovery/status")
    assert response.status_code == 200, response.json()
    data = response.json()
    assert data["discovery_enabled"] is True
    # Latest run should be the most recent (dr_status002)
    assert data["latest_run"]["discovery_run_id"] == "dr_status002"
    assert data["latest_run"]["status"] == "degraded"
    assert data["latest_run"]["source"] == "manual_rerun"
    assert data["latest_run"]["total_services_discovered"] == 3
    assert data["total_runs"] == 2
    assert len(data["recent_runs"]) == 2


def test_discovery_status_respects_limit(client, db_session):
    """Status endpoint respects the limit query parameter."""
    for i in range(10):
        _create_discovery_run(
            db_session,
            discovery_run_id=f"dr_limit{i:03d}",
            status="succeeded",
            trigger_type="automatic",
        )

    response = client.get("/api/discovery/status?limit=3")
    assert response.status_code == 200, response.json()
    data = response.json()
    assert len(data["recent_runs"]) == 3
    assert data["total_runs"] == 3  # limited to 3 returned


# ---------------------------------------------------------------------------
# GET /api/discovery/services
# ---------------------------------------------------------------------------


def test_discovery_services_no_data(client, db_session):
    """Returns empty list when no discovery data exists."""
    response = client.get("/api/discovery/services")
    assert response.status_code == 200, response.json()
    data = response.json()
    assert data["services"] == []
    assert data["total"] == 0


def test_discovery_services_from_run_summary(client, db_session):
    """Returns services from the latest run that has service data."""
    _create_discovery_run(
        db_session,
        discovery_run_id="dr_svc001",
        status="succeeded",
        summary={
            "services": [
                {
                    "name": "checkout",
                    "namespace": "production",
                    "labels": {"app": "checkout", "team": "payments"},
                    "sources": ["k8s_workload", "k8s_service"],
                },
                {
                    "name": "payment-gateway",
                    "namespace": "production",
                    "labels": {"app": "payment"},
                    "sources": ["k8s_workload"],
                },
            ],
        },
    )

    response = client.get("/api/discovery/services")
    assert response.status_code == 200, response.json()
    data = response.json()
    assert data["total"] == 2
    assert data["services"][0]["name"] == "checkout"
    assert data["services"][0]["namespace"] == "production"
    assert "k8s_workload" in data["services"][0]["sources"]


# ---------------------------------------------------------------------------
# GET /api/discovery/metrics
# ---------------------------------------------------------------------------


def test_discovery_metrics_no_data(client, db_session):
    """Returns empty list when no metric data exists."""
    response = client.get("/api/discovery/metrics")
    assert response.status_code == 200, response.json()
    data = response.json()
    assert data["metrics"] == []
    assert data["total"] == 0


def test_discovery_metrics_from_run_summary(client, db_session):
    """Returns metric mappings from the latest run summary."""
    _create_discovery_run(
        db_session,
        discovery_run_id="dr_met001",
        status="succeeded",
        summary={
            "metric_mappings": [
                {
                    "semantic_type": "latency",
                    "metric_name": "http_request_duration_seconds",
                    "status": "available",
                    "confidence": 0.95,
                    "promql_template": 'histogram_quantile(0.99, sum(rate({metric}[5m])) by (le, {service_label}))',
                    "service_label": "service",
                    "required_labels": ["le"],
                },
                {
                    "semantic_type": "error_rate",
                    "metric_name": "http_requests_total",
                    "status": "degraded",
                    "confidence": 0.65,
                    "promql_template": "",
                    "service_label": "service",
                    "degraded_reason": "no status label found",
                    "alternatives": ["http_request_duration_seconds_count"],
                },
            ],
        },
    )

    response = client.get("/api/discovery/metrics")
    assert response.status_code == 200, response.json()
    data = response.json()
    assert data["total"] == 2
    assert data["metrics"][0]["semantic_type"] == "latency"
    assert data["metrics"][0]["status"] == "available"
    assert data["metrics"][1]["status"] == "degraded"


# ---------------------------------------------------------------------------
# GET /api/discovery/topology
# ---------------------------------------------------------------------------


def test_discovery_topology_no_data(client, db_session):
    """Returns empty topology when no data exists."""
    response = client.get("/api/discovery/topology")
    assert response.status_code == 200, response.json()
    data = response.json()
    assert data["workload_bindings"] == []
    assert data["service_edges"] == []


def test_discovery_topology_from_run_summary(client, db_session):
    """Returns topology from the latest run summary."""
    _create_discovery_run(
        db_session,
        discovery_run_id="dr_topo001",
        status="succeeded",
        summary={
            "workload_bindings": [
                {
                    "service_name": "checkout",
                    "workload_name": "checkout-deployment",
                    "workload_kind": "Deployment",
                    "namespace": "production",
                },
            ],
            "service_edges": [
                {
                    "source_service": "checkout",
                    "target_service": "payment-gateway",
                    "edge_type": "trace",
                    "confidence": 0.85,
                    "evidence": {"trace_count": 150},
                },
            ],
        },
    )

    response = client.get("/api/discovery/topology")
    assert response.status_code == 200, response.json()
    data = response.json()
    assert len(data["workload_bindings"]) == 1
    assert data["workload_bindings"][0]["service_name"] == "checkout"
    assert data["workload_bindings"][0]["workload_kind"] == "Deployment"
    assert len(data["service_edges"]) == 1
    assert data["service_edges"][0]["edge_type"] == "trace"


# ---------------------------------------------------------------------------
# GET /api/discovery/capabilities
# ---------------------------------------------------------------------------


def test_discovery_capabilities_no_data(client, db_session):
    """Returns empty capability matrix when no data exists."""
    response = client.get("/api/discovery/capabilities")
    assert response.status_code == 200, response.json()
    data = response.json()
    assert data["capabilities"] == []
    assert data["total_services"] == 0


def test_discovery_capabilities_from_run_summary(client, db_session):
    """Returns capabilities from the latest run summary."""
    _create_discovery_run(
        db_session,
        discovery_run_id="dr_cap001",
        status="succeeded",
        summary={
            "capability_matrix": [
                {
                    "service_name": "checkout",
                    "metrics_available": True,
                    "logs_available": True,
                    "traces_available": True,
                    "k8s_accessible": True,
                    "metric_mappings": [
                        {
                            "semantic_type": "latency",
                            "metric_name": "http_request_duration_seconds",
                            "status": "available",
                            "confidence": 0.95,
                        },
                    ],
                    "capability_gaps": [],
                },
                {
                    "service_name": "legacy-app",
                    "metrics_available": False,
                    "logs_available": True,
                    "traces_available": False,
                    "k8s_accessible": False,
                    "metric_mappings": [],
                    "capability_gaps": ["metrics_unavailable", "traces_unavailable", "k8s_inaccessible"],
                },
            ],
        },
    )

    response = client.get("/api/discovery/capabilities")
    assert response.status_code == 200, response.json()
    data = response.json()
    assert data["total_services"] == 2
    assert data["capabilities"][0]["service_name"] == "checkout"
    assert data["capabilities"][0]["metrics_available"] is True
    assert len(data["capabilities"][0]["capability_gaps"]) == 0
    assert data["capabilities"][1]["service_name"] == "legacy-app"
    assert len(data["capabilities"][1]["capability_gaps"]) == 3


def test_discovery_capabilities_from_published_config(client, db_session):
    """Falls back to published config snapshot when run summary has no capabilities."""
    from packages.common.ids import new_id
    from packages.common.time import utc_now
    from packages.db.models import EffectiveConfigVersion

    # Create a published config with capability data.
    ecv = EffectiveConfigVersion(
        version_id=new_id("ecv_"),
        version_number=1,
        status="published",
        config_snapshot={
            "prometheus_url": "http://prom:9090",
            "capabilities": [
                {
                    "service_name": "from-config-svc",
                    "metrics_available": True,
                    "logs_available": False,
                    "traces_available": True,
                    "k8s_accessible": True,
                    "metric_mappings": [],
                    "capability_gaps": ["logs_unavailable"],
                },
            ],
        },
        published_at=utc_now(),
    )
    db_session.add(ecv)
    db_session.flush()

    # Also create a run without capabilities in summary.
    _create_discovery_run(
        db_session,
        discovery_run_id="dr_nocap001",
        status="succeeded",
        summary={"total_services_discovered": 0},
    )

    response = client.get("/api/discovery/capabilities")
    assert response.status_code == 200, response.json()
    data = response.json()
    assert data["total_services"] >= 1
    # Should find the capability from the published config.
    services = [c["service_name"] for c in data["capabilities"]]
    assert "from-config-svc" in services
