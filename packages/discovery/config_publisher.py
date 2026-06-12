"""ConfigPublisher — publish, rollback, revoke EffectiveConfigVersions.

M3 PR 3.6: Manages the lifecycle of published effective configurations.
All operations write to the immutable audit log. Published configs use
a stale-warning strategy (default 30 days) — configs are never hard-expired.

Priority: env > active override > profile > published > safe default.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy.orm import Session

from packages.common.ids import new_id
from packages.common.time import utc_now
from packages.db.models import EffectiveConfigVersion
from packages.db.repositories.audit_logs import AuditLogRepository
from packages.db.repositories.effective_configs import EffectiveConfigRepository


class ConfigPublisherError(Exception):
    """Base exception for config publishing errors."""


class ConfigPublishError(ConfigPublisherError):
    """Error during config publish."""


class ConfigRollbackError(ConfigPublisherError):
    """Error during config rollback."""


class ConfigRevokeError(ConfigPublisherError):
    """Error during config revoke."""


class ConfigPublisher:
    """Manages EffectiveConfigVersion lifecycle: publish, rollback, revoke.

    Usage::

        publisher = ConfigPublisher(db_session)
        version = publisher.publish(
            config_snapshot=config_dict,
            proposal_id="dp_xxx",
            published_by="operator-key-1",
        )
        publisher.rollback(version.version_id, rolled_back_by="operator-key-1")
        publisher.revoke(version.version_id, revoked_by="operator-key-1")
    """

    def __init__(
        self,
        db: Session,
        *,
        audit_repo: AuditLogRepository | None = None,
    ) -> None:
        self._db = db
        self._repo = EffectiveConfigRepository(db)
        self._audit = audit_repo or AuditLogRepository(db)

    # ------------------------------------------------------------------
    # Publish
    # ------------------------------------------------------------------

    def publish(
        self,
        *,
        config_snapshot: dict[str, Any],
        proposal_id: str | None = None,
        published_by: str | None = None,
        stale_after_days: int = 30,
    ) -> EffectiveConfigVersion:
        """Publish a new EffectiveConfigVersion.

        The previous published version is superseded. The new version becomes
        the active published config that workers read.

        Args:
            config_snapshot: Full effective config snapshot.
            proposal_id: Optional DiscoveryProposal this config is based on.
            published_by: Identity of the publisher (API key ID).
            stale_after_days: Days before this config is considered stale.

        Returns:
            The newly created EffectiveConfigVersion.

        Raises:
            ConfigPublishError: If publish fails.
        """
        # Determine next version number.
        latest = self._repo.get_latest_published()
        next_version = 1 if latest is None else latest.version_number + 1

        # Supersede the previous published version.
        if latest is not None:
            latest.status = "superseded"
            latest.rolled_back_at = utc_now()
            self._db.flush()

        # Calculate stale warning timestamp.
        stale_warning_at = utc_now() + timedelta(days=stale_after_days)

        # Create new version.
        version = EffectiveConfigVersion(
            version_id=new_id("ecv_"),
            proposal_id=proposal_id,
            version_number=next_version,
            status="published",
            config_snapshot=config_snapshot,
            published_at=utc_now(),
            published_by=published_by,
            stale_after_days=stale_after_days,
            stale_warning_at=stale_warning_at,
        )
        self._db.add(version)
        self._db.flush()

        # Audit.
        self._audit.create_config_audit(
            action="config.publish",
            resource_type="effective_config_version",
            resource_id=version.version_id,
            actor=published_by or "system",
            details={
                "version_number": next_version,
                "proposal_id": proposal_id,
                "stale_after_days": stale_after_days,
            },
        )

        return version

    # ------------------------------------------------------------------
    # Rollback
    # ------------------------------------------------------------------

    def rollback(
        self,
        version_id: str,
        *,
        rolled_back_by: str | None = None,
    ) -> EffectiveConfigVersion:
        """Rollback to the previous published version.

        The specified version is rolled back, and the most recent
        superseded version (if any) is re-published.

        Returns:
            The newly re-published version (previous superseded).
        """
        current = self._repo.get_by_id(version_id)
        if current is None:
            raise ConfigRollbackError(
                f"Version '{version_id}' not found"
            )
        if current.status != "published":
            raise ConfigRollbackError(
                f"Cannot rollback version with status '{current.status}'"
            )

        # Mark current as rolled back.
        current.status = "rolled_back"
        current.rolled_back_at = utc_now()
        self._db.flush()

        # Find the most recent superseded version to restore.
        all_versions = self._repo.list_published(limit=50)
        previous = None
        for v in all_versions:
            if v.status == "superseded" and v.version_id != version_id:
                previous = v
                break

        if previous is None:
            # No previous version to restore — publish a minimal empty config.
            previous = EffectiveConfigVersion(
                version_id=new_id("ecv_"),
                version_number=current.version_number + 1,
                status="published",
                config_snapshot={},
                published_at=utc_now(),
                published_by=rolled_back_by,
                stale_after_days=30,
                stale_warning_at=utc_now() + timedelta(days=30),
            )
            self._db.add(previous)
        else:
            # Reactivate the previous version under a new version_number.
            previous.status = "published"
            previous.published_by = rolled_back_by
            previous.published_at = utc_now()

        self._db.flush()

        # Audit.
        self._audit.create_config_audit(
            action="config.rollback",
            resource_type="effective_config_version",
            resource_id=version_id,
            actor=rolled_back_by or "system",
            details={
                "rolled_back_from_version": current.version_number,
                "restored_version": previous.version_number,
            },
        )

        return previous

    # ------------------------------------------------------------------
    # Revoke
    # ------------------------------------------------------------------

    def revoke(
        self,
        version_id: str,
        *,
        revoked_by: str | None = None,
        reason: str | None = None,
    ) -> EffectiveConfigVersion:
        """Revoke a published config version.

        The version is marked as revoked and removed from worker selection.
        If no other published version exists, workers will have no config
        and must rely on env/profile/defaults.
        """
        version = self._repo.get_by_id(version_id)
        if version is None:
            raise ConfigRevokeError(
                f"Version '{version_id}' not found"
            )
        if version.status not in ("published", "superseded"):
            raise ConfigRevokeError(
                f"Cannot revoke version with status '{version.status}'"
            )

        version.status = "revoked"
        version.revoked_at = utc_now()
        self._db.flush()

        # Audit.
        self._audit.create_config_audit(
            action="config.revoke",
            resource_type="effective_config_version",
            resource_id=version_id,
            actor=revoked_by or "system",
            details={
                "version_number": version.version_number,
                "reason": reason,
            },
        )

        return version

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def get_current_config(self) -> dict[str, Any] | None:
        """Get the currently published config snapshot."""
        latest = self._repo.get_latest_published()
        if latest is None:
            return None
        return latest.config_snapshot  # type: ignore[no-any-return]

    def get_version(self, version_id: str) -> EffectiveConfigVersion | None:
        """Get a specific config version."""
        return self._repo.get_by_id(version_id)

    def list_versions(self, limit: int = 10) -> list[EffectiveConfigVersion]:
        """List recent published versions."""
        return list(self._repo.list_published(limit))

    def is_stale(self, version: EffectiveConfigVersion) -> bool:
        """Check if a config version is past its stale warning threshold."""
        if version.stale_warning_at is None:
            return False
        return utc_now() > version.stale_warning_at
