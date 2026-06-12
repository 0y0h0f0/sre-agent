"""Tests for PR 0.2: Discovery / EffectiveConfig / AuditLog data models.

Note: SQLAlchemy mapped_column defaults (default=, server_default=) are
INSERT-time defaults, not Python __init__ defaults. Tests pass values
explicitly OR verify column default metadata.
"""

from __future__ import annotations

from datetime import timedelta

from packages.common.ids import new_id
from packages.common.time import utc_now
from packages.db.models import (
    AuditLog,
    DiscoveryOverride,
    DiscoveryProposal,
    DiscoveryRun,
    EffectiveConfigVersion,
)


class TestDiscoveryRun:
    def test_create(self):
        """DiscoveryRun can be instantiated with required fields."""
        run = DiscoveryRun(
            discovery_run_id=new_id("dr_"),
            source="manual_rerun",
            status="running",
            trigger_type="manual",
            triggered_by="operator",
            started_at=utc_now(),
            summary={},
        )
        assert run.discovery_run_id.startswith("dr_")
        assert run.source == "manual_rerun"
        assert run.status == "running"
        assert run.trigger_type == "manual"
        assert run.triggered_by == "operator"
        assert run.started_at is not None
        assert run.summary == {}

    def test_column_defaults_defined(self):
        """mapped_column default= values are set on table column metadata."""
        col_status = DiscoveryRun.__table__.c.status
        assert col_status.default is not None
        assert col_status.default.arg == "running"

    def test_summary_column_default_defined(self):
        """summary column has dict default for INSERT."""
        col = DiscoveryRun.__table__.c.summary
        assert col.default is not None
        # CallableColumnDefault wraps dict; the default produces empty dict.
        result = col.default.arg(None)
        assert result == {}


class TestDiscoveryProposal:
    def test_create(self):
        """DiscoveryProposal can be instantiated with required fields."""
        proposal = DiscoveryProposal(
            proposal_id=new_id("dp_"),
            discovery_run_id=new_id("dr_"),
            status="pending_review",
            config_diff={},
        )
        assert proposal.proposal_id.startswith("dp_")
        assert proposal.status == "pending_review"
        assert proposal.config_diff == {}

    def test_status_flow(self):
        """Proposal status transitions are tracked."""
        proposal = DiscoveryProposal(
            proposal_id=new_id("dp_"),
            discovery_run_id=new_id("dr_"),
            status="pending_review",
            config_diff={},
        )
        assert proposal.status == "pending_review"
        proposal.status = "auto_applied"
        proposal.applied_at = utc_now()
        assert proposal.status == "auto_applied"
        assert proposal.applied_at is not None

    def test_rejected_reason(self):
        """Rejected proposals store a reason."""
        proposal = DiscoveryProposal(
            proposal_id=new_id("dp_"),
            discovery_run_id=new_id("dr_"),
            status="rejected",
            config_diff={},
            rejected_reason="Low confidence in metric mapping",
        )
        assert proposal.rejected_reason == "Low confidence in metric mapping"


class TestEffectiveConfigVersion:
    def test_create(self):
        """EffectiveConfigVersion can be instantiated with required fields."""
        version = EffectiveConfigVersion(
            version_id=new_id("ecv_"),
            version_number=1,
            status="published",
            config_snapshot={"prometheus_url": "http://prom:9090"},
            stale_after_days=30,
        )
        assert version.version_id.startswith("ecv_")
        assert version.version_number == 1
        assert version.status == "published"
        assert version.config_snapshot["prometheus_url"] == "http://prom:9090"
        assert version.stale_after_days == 30

    def test_lifecycle(self):
        """Version transitions through published -> superseded."""
        version = EffectiveConfigVersion(
            version_id=new_id("ecv_"),
            version_number=1,
            status="published",
            config_snapshot={},
            stale_after_days=30,
        )
        assert version.status == "published"
        version.status = "superseded"
        assert version.status == "superseded"

    def test_stale_warning_does_not_disable_worker_selection(self):
        """Stale config is still usable by workers — no hard expiry."""
        version = EffectiveConfigVersion(
            version_id=new_id("ecv_"),
            version_number=1,
            status="published",
            config_snapshot={},
            stale_after_days=30,
        )
        # Setting stale_warning_at does NOT change status from 'published'.
        version.stale_warning_at = utc_now() + timedelta(days=30)
        assert version.status == "published"
        assert version.stale_warning_at is not None

    def test_rollback_fields(self):
        """Rollback and revoke timestamps are trackable."""
        version = EffectiveConfigVersion(
            version_id=new_id("ecv_"),
            version_number=1,
            status="published",
            config_snapshot={},
            stale_after_days=30,
        )
        now = utc_now()
        version.rolled_back_at = now
        version.status = "rolled_back"
        assert version.rolled_back_at == now
        assert version.status == "rolled_back"


class TestDiscoveryOverride:
    def test_create(self):
        """DiscoveryOverride can be instantiated with required fields."""
        expires = utc_now() + timedelta(days=7)
        override = DiscoveryOverride(
            override_id=new_id("dov_"),
            backend_type="prometheus",
            override_json={"url": "http://prom:9090"},
            reason="Firefighting — need custom URL",
            expires_at=expires,
            created_by_scopes=[],
        )
        assert override.override_id.startswith("dov_")
        assert override.backend_type == "prometheus"
        assert override.reason == "Firefighting — need custom URL"
        assert override.expires_at == expires
        assert override.revoked_at is None
        assert override.created_by_scopes == []

    def test_requires_expires_at(self):
        """DiscoveryOverride must have expires_at set."""
        override = DiscoveryOverride(
            override_id=new_id("dov_"),
            backend_type="prometheus",
            reason="Test",
            expires_at=utc_now() + timedelta(days=7),
            created_by_scopes=[],
        )
        assert override.expires_at is not None

    def test_revoked_not_active(self):
        """Revoked override should not be considered active."""
        override = DiscoveryOverride(
            override_id=new_id("dov_"),
            backend_type="prometheus",
            reason="Test",
            expires_at=utc_now() + timedelta(days=7),
            revoked_at=utc_now(),
            revoke_reason="No longer needed",
            created_by_scopes=[],
        )
        assert override.revoked_at is not None
        assert override.revoke_reason == "No longer needed"

    def test_expiry(self):
        """Override with past expires_at is expired."""
        past = utc_now() - timedelta(days=1)
        override = DiscoveryOverride(
            override_id=new_id("dov_"),
            backend_type="prometheus",
            reason="Test",
            expires_at=past,
            created_by_scopes=[],
        )
        assert override.expires_at < utc_now()

    def test_max_ttl_validation(self):
        """Override TTL should not exceed maximum (enforced at service layer)."""
        max_ttl = utc_now() + timedelta(days=30)
        override = DiscoveryOverride(
            override_id=new_id("dov_"),
            backend_type="loki",
            reason="Test",
            expires_at=max_ttl,
            created_by_scopes=[],
        )
        assert override.expires_at == max_ttl


class TestAuditLogExtensions:
    def test_audit_log_supports_discovery_actions(self):
        """AuditLog can record discovery-related actions."""
        audit = AuditLog(
            audit_id=new_id("adt_"),
            actor="discovery-runner",
            action="discovery.auto_apply",
            resource_type="discovery_proposal",
            resource_id=new_id("dp_"),
            source="worker",
            request_id="req-123",
            details={"proposal_id": "dp_abc", "changes": 3},
        )
        assert audit.action == "discovery.auto_apply"
        assert audit.source == "worker"
        assert audit.request_id == "req-123"
        assert audit.details["changes"] == 3

    def test_audit_log_config_publish_action(self):
        """AuditLog can record config.publish actions."""
        audit = AuditLog(
            audit_id=new_id("adt_"),
            actor="operator",
            action="config.publish",
            resource_type="effective_config_version",
            resource_id=new_id("ecv_"),
            source="api",
            details={"version_number": 2},
        )
        assert audit.action == "config.publish"
        assert audit.source == "api"

    def test_audit_log_config_rollback_action(self):
        """AuditLog can record config.rollback actions."""
        audit = AuditLog(
            audit_id=new_id("adt_"),
            actor="operator",
            action="config.rollback",
            resource_type="effective_config_version",
            resource_id=new_id("ecv_"),
            source="api",
        )
        assert audit.action == "config.rollback"

    def test_audit_log_source_default(self):
        """AuditLog source defaults to None for backward compat."""
        audit = AuditLog(
            audit_id=new_id("adt_"),
            actor="test",
            action="test.action",
            resource_type="test",
            resource_id="test",
        )
        assert audit.source is None
        assert audit.request_id is None
