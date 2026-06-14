"""Tests for M3 PR 3.4: DiscoveryStore."""

from __future__ import annotations

from sqlalchemy.orm import Session

from packages.db.models import DiscoveryProposal, DiscoveryRun
from packages.discovery.models import (
    DiscoveryResult,
    MetricMapping,
    ServiceEdgeModel,
    ServiceInfo,
    WorkloadBindingModel,
)
from packages.discovery.store import DiscoveryStore


class TestDiscoveryStore:
    def test_create_run(self, db_session: Session):
        """DiscoveryStore creates a DiscoveryRun in running state."""
        store = DiscoveryStore(db_session)
        run = store.create_run(source="scheduled", trigger_type="automatic")
        db_session.flush()

        assert isinstance(run, DiscoveryRun)
        assert run.source == "scheduled"
        assert run.status == "running"
        assert run.trigger_type == "automatic"

    def test_finish_run_succeeded(self, db_session: Session):
        """finish_run updates status and summary."""
        store = DiscoveryStore(db_session)
        run = store.create_run(source="manual_rerun")
        db_session.flush()

        result = DiscoveryResult(
            run_id=run.discovery_run_id,
            total_services_discovered=3,
            total_metrics_scanned=5,
            duration_seconds=2.5,
            status="succeeded",
        )
        store.finish_run(run, result, status="succeeded")
        db_session.flush()

        assert run.status == "succeeded"
        assert run.finished_at is not None
        assert run.summary["total_services_discovered"] == 3

    def test_finish_run_persists_discovery_detail_summary(self, db_session: Session):
        """finish_run stores details consumed by discovery read APIs."""
        store = DiscoveryStore(db_session)
        run = store.create_run(source="manual_rerun")
        db_session.flush()

        result = DiscoveryResult(
            run_id=run.discovery_run_id,
            services=[
                ServiceInfo(
                    name="checkout",
                    namespace="prod",
                    labels={"app": "checkout"},
                    sources=["k8s_service"],
                )
            ],
            metric_mappings=[
                MetricMapping(
                    semantic_type="latency",
                    metric_name="http_request_duration_seconds_bucket",
                    status="available",
                )
            ],
            workload_bindings=[
                WorkloadBindingModel(
                    service_name="checkout",
                    workload_name="checkout",
                    workload_kind="Deployment",
                    namespace="prod",
                )
            ],
            service_edges=[
                ServiceEdgeModel(
                    source_service="checkout",
                    target_service="payments.prod",
                    edge_type="configmap",
                    confidence=0.5,
                    evidence={"configmap": "checkout-config"},
                )
            ],
            status="succeeded",
        )
        store.finish_run(run, result, status="succeeded")
        db_session.flush()

        assert run.summary["services"][0]["name"] == "checkout"
        assert run.summary["metric_mappings"][0]["semantic_type"] == "latency"
        assert run.summary["workload_bindings"][0]["service_name"] == "checkout"
        assert run.summary["service_edges"][0]["edge_type"] == "configmap"

    def test_finish_run_degraded(self, db_session: Session):
        """finish_run records degraded status."""
        store = DiscoveryStore(db_session)
        run = store.create_run(source="scheduled")
        db_session.flush()

        result = DiscoveryResult(
            run_id=run.discovery_run_id,
            warnings=["prometheus unreachable"],
            degraded_signals=["prometheus_unavailable"],
            status="degraded",
        )
        store.finish_run(run, result, status="degraded")
        db_session.flush()

        assert run.status == "degraded"
        assert "prometheus_unavailable" in run.summary.get("degraded_signals", [])

    def test_create_proposal(self, db_session: Session):
        """DiscoveryStore creates a DiscoveryProposal."""
        store = DiscoveryStore(db_session)
        run = store.create_run(source="scheduled")
        db_session.flush()

        proposal = store.create_proposal(
            discovery_run_id=run.discovery_run_id,
            config_diff={"prometheus_url": {"old": None, "new": "http://prom:9090"}},
            confidence=0.85,
        )
        db_session.flush()

        assert isinstance(proposal, DiscoveryProposal)
        assert proposal.status == "pending_review"
        assert proposal.confidence == 0.85

    def test_proposal_diff_matches_changes(self, db_session: Session):
        """Config diff is stored correctly."""
        store = DiscoveryStore(db_session)
        run = store.create_run(source="scheduled")
        db_session.flush()

        diff = {
            "prometheus_url": {"action": "add", "value": "http://prom:9090"},
            "metrics_service_label": {"action": "update", "old": "service", "new": "app"},
        }
        proposal = store.create_proposal(
            discovery_run_id=run.discovery_run_id,
            config_diff=diff,
            confidence=0.90,
        )
        db_session.flush()

        assert proposal.config_diff == diff
        assert proposal.config_diff["prometheus_url"]["action"] == "add"

    def test_list_pending_proposals(self, db_session: Session):
        """Pending proposals can be listed."""
        store = DiscoveryStore(db_session)
        run = store.create_run(source="scheduled")
        db_session.flush()

        store.create_proposal(discovery_run_id=run.discovery_run_id)
        store.create_proposal(
            discovery_run_id=run.discovery_run_id,
            status="auto_applied",
        )
        db_session.flush()

        pending = store.list_pending_proposals()
        assert len(pending) == 1
        assert pending[0].status == "pending_review"

    def test_update_proposal_status(self, db_session: Session):
        """Proposal status can be updated after review."""
        store = DiscoveryStore(db_session)
        run = store.create_run(source="scheduled")
        db_session.flush()
        proposal = store.create_proposal(discovery_run_id=run.discovery_run_id)
        db_session.flush()

        store.update_proposal_status(
            proposal,
            "auto_applied",
            reviewed_by="operator-1",
        )
        db_session.flush()

        assert proposal.status == "auto_applied"
        assert proposal.reviewed_by == "operator-1"
        assert proposal.applied_at is not None

    def test_update_proposal_rejected(self, db_session: Session):
        """Proposal can be rejected with reason."""
        store = DiscoveryStore(db_session)
        run = store.create_run(source="scheduled")
        db_session.flush()
        proposal = store.create_proposal(discovery_run_id=run.discovery_run_id)
        db_session.flush()

        store.update_proposal_status(
            proposal,
            "rejected",
            rejected_reason="Backend URL not in allowlist",
        )
        db_session.flush()

        assert proposal.status == "rejected"
        assert proposal.rejected_reason == "Backend URL not in allowlist"

    def test_supersede_proposals(self, db_session: Session):
        """Pending proposals can be superseded."""
        store = DiscoveryStore(db_session)
        run = store.create_run(source="scheduled")
        db_session.flush()

        p1 = store.create_proposal(discovery_run_id=run.discovery_run_id)
        p2 = store.create_proposal(discovery_run_id=run.discovery_run_id)
        db_session.flush()

        store.supersede_proposals(run.discovery_run_id)
        db_session.flush()

        # Refresh from DB.
        db_session.refresh(p1)
        db_session.refresh(p2)
        assert p1.status == "superseded"
        assert p2.status == "superseded"

    def test_list_recent_runs(self, db_session: Session):
        """Recent runs can be listed."""
        store = DiscoveryStore(db_session)
        for i in range(5):
            run = store.create_run(source="scheduled")
            result = DiscoveryResult(
                run_id=run.discovery_run_id,
                total_services_discovered=i,
                status="succeeded",
            )
            store.finish_run(run, result)
        db_session.flush()

        runs = store.list_recent_runs(limit=3)
        assert len(runs) == 3
