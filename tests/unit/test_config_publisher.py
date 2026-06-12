"""Tests for M3 PR 3.6: ConfigPublisher."""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from packages.discovery.config_publisher import (
    ConfigPublisher,
    ConfigRevokeError,
    ConfigRollbackError,
)


class TestPublish:
    def test_publish_creates_version(self, db_session: Session):
        """Publish creates an EffectiveConfigVersion."""
        publisher = ConfigPublisher(db_session)
        version = publisher.publish(
            config_snapshot={
                "prometheus_url": "http://prom:9090",
                "loki_url": "http://loki:3100",
            },
            published_by="operator-1",
        )

        assert version.version_id.startswith("ecv_")
        assert version.version_number == 1
        assert version.status == "published"
        assert version.config_snapshot["prometheus_url"] == "http://prom:9090"

    def test_publish_supersedes_previous_version(self, db_session: Session):
        """Publishing a new version supersedes the previous one."""
        publisher = ConfigPublisher(db_session)
        v1 = publisher.publish(
            config_snapshot={"prometheus_url": "http://v1:9090"},
            published_by="operator-1",
        )
        v2 = publisher.publish(
            config_snapshot={"prometheus_url": "http://v2:9090"},
            published_by="operator-1",
        )

        assert v2.version_number == v1.version_number + 1
        assert v2.status == "published"
        # v1 should now be superseded.
        db_session.refresh(v1)
        assert v1.status == "superseded"

    def test_publish_creates_audit_log(self, db_session: Session):
        """Publish writes an audit log entry."""
        publisher = ConfigPublisher(db_session)
        publisher.publish(
            config_snapshot={"prometheus_url": "http://prom:9090"},
            published_by="operator-1",
        )
        db_session.flush()

        from packages.db.repositories.audit_logs import AuditLogRepository
        repo = AuditLogRepository(db_session)
        logs = repo.query_by_action("config.publish")
        assert len(logs) >= 1
        assert logs[0].actor == "operator-1"


class TestRollback:
    def test_rollback_restores_previous_version(self, db_session: Session):
        """Rollback restores the most recent superseded version."""
        publisher = ConfigPublisher(db_session)
        _v1 = publisher.publish(
            config_snapshot={"prometheus_url": "http://v1:9090"},
            published_by="operator-1",
        )
        v2 = publisher.publish(
            config_snapshot={"prometheus_url": "http://v2:9090"},
            published_by="operator-1",
        )

        restored = publisher.rollback(v2.version_id, rolled_back_by="operator-1")

        db_session.refresh(v2)
        assert v2.status == "rolled_back"
        assert restored.status == "published"

    def test_rollback_creates_audit_log(self, db_session: Session):
        """Rollback writes an audit log entry."""
        publisher = ConfigPublisher(db_session)
        _v1 = publisher.publish(
            config_snapshot={"prometheus_url": "http://v1:9090"},
            published_by="operator-1",
        )
        v2 = publisher.publish(
            config_snapshot={"prometheus_url": "http://v2:9090"},
            published_by="operator-1",
        )
        publisher.rollback(v2.version_id, rolled_back_by="operator-1")
        db_session.flush()

        from packages.db.repositories.audit_logs import AuditLogRepository
        repo = AuditLogRepository(db_session)
        logs = repo.query_by_action("config.rollback")
        assert len(logs) >= 1

    def test_rollback_nonexistent_version(self, db_session: Session):
        """Rollback of non-existent version raises error."""
        publisher = ConfigPublisher(db_session)
        with pytest.raises(ConfigRollbackError):
            publisher.rollback("ecv_nonexistent")

    def test_rollback_non_published_version(self, db_session: Session):
        """Rollback of already rolled-back version raises error."""
        publisher = ConfigPublisher(db_session)
        v1 = publisher.publish(
            config_snapshot={"prometheus_url": "http://v1:9090"},
            published_by="operator-1",
        )
        publisher.rollback(v1.version_id, rolled_back_by="operator-1")

        with pytest.raises(ConfigRollbackError):
            publisher.rollback(v1.version_id)


class TestRevoke:
    def test_revoke_removes_from_worker_selection(self, db_session: Session):
        """Revoke marks the version as revoked."""
        publisher = ConfigPublisher(db_session)
        v1 = publisher.publish(
            config_snapshot={"prometheus_url": "http://prom:9090"},
            published_by="operator-1",
        )
        publisher.revoke(v1.version_id, revoked_by="operator-1")
        db_session.refresh(v1)

        assert v1.status == "revoked"
        assert v1.revoked_at is not None

    def test_revoke_creates_audit_log(self, db_session: Session):
        """Revoke writes an audit log entry."""
        publisher = ConfigPublisher(db_session)
        v1 = publisher.publish(
            config_snapshot={"prometheus_url": "http://prom:9090"},
            published_by="operator-1",
        )
        publisher.revoke(v1.version_id, revoked_by="operator-1")
        db_session.flush()

        from packages.db.repositories.audit_logs import AuditLogRepository
        repo = AuditLogRepository(db_session)
        logs = repo.query_by_action("config.revoke")
        assert len(logs) >= 1

    def test_revoke_nonexistent_version(self, db_session: Session):
        """Revoke of non-existent version raises error."""
        publisher = ConfigPublisher(db_session)
        with pytest.raises(ConfigRevokeError):
            publisher.revoke("ecv_nonexistent")


class TestRead:
    def test_get_current_config(self, db_session: Session):
        """get_current_config returns the latest published snapshot."""
        publisher = ConfigPublisher(db_session)
        publisher.publish(
            config_snapshot={"prometheus_url": "http://prom:9090"},
            published_by="operator-1",
        )
        db_session.flush()

        config = publisher.get_current_config()
        assert config is not None
        assert config["prometheus_url"] == "http://prom:9090"

    def test_get_current_config_empty(self, db_session: Session):
        """get_current_config returns None when no published version exists."""
        publisher = ConfigPublisher(db_session)
        assert publisher.get_current_config() is None

    def test_list_versions(self, db_session: Session):
        """list_versions returns the currently published version (superseded are excluded)."""
        publisher = ConfigPublisher(db_session)
        for i in range(3):
            publisher.publish(
                config_snapshot={"prometheus_url": f"http://v{i}:9090"},
                published_by="operator-1",
            )
        db_session.flush()

        # Only the latest version has status="published"; previous are superseded.
        versions = publisher.list_versions(limit=5)
        assert len(versions) == 1
        assert versions[0].status == "published"
        assert versions[0].config_snapshot["prometheus_url"] == "http://v2:9090"


class TestStaleConfig:
    def test_stale_config_still_used_with_warning(self, db_session: Session):
        """Stale config is still returned but flagged by is_stale()."""

        publisher = ConfigPublisher(db_session)
        version = publisher.publish(
            config_snapshot={"prometheus_url": "http://prom:9090"},
            published_by="operator-1",
            stale_after_days=0,  # immediately stale
        )
        db_session.flush()

        # Config is still published (not revoked).
        assert version.status == "published"
        # But is_stale returns True.
        assert publisher.is_stale(version) is True
        # get_current_config still returns it.
        config = publisher.get_current_config()
        assert config is not None

    def test_version_staleness_warning_after_threshold(self, db_session: Session):
        """After stale threshold, is_stale() returns True."""
        from datetime import timedelta

        from packages.common.time import utc_now

        publisher = ConfigPublisher(db_session)
        version = publisher.publish(
            config_snapshot={"prometheus_url": "http://prom:9090"},
            published_by="operator-1",
            stale_after_days=1,
        )
        # Manually advance stale_warning_at to simulate passage of time.
        version.stale_warning_at = utc_now() - timedelta(hours=1)
        db_session.flush()

        assert publisher.is_stale(version) is True
