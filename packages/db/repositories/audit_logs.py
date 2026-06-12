"""Repository for audit_logs table — write-ahead operation audit.

Audit logs are immutable once created. Repository does not expose update/delete
methods. DB-level trigger enforcement should be added in production.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from packages.common.ids import new_id
from packages.db.models import AuditLog


class AuditLogRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def create(
        self,
        *,
        incident_id: str | None,
        actor: str,
        action: str,
        resource_type: str,
        resource_id: str,
        details: dict[str, Any] | None = None,
        source: str | None = None,
        request_id: str | None = None,
    ) -> AuditLog:
        audit = AuditLog(
            audit_id=new_id("adt_"),
            incident_id=incident_id,
            actor=actor,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            details=details or {},
            source=source,
            request_id=request_id,
        )
        self.db.add(audit)
        return audit

    def create_config_audit(
        self,
        *,
        actor: str,
        action: str,
        resource_type: str,
        resource_id: str,
        details: dict[str, Any] | None = None,
        source: str = "api",
        request_id: str | None = None,
    ) -> AuditLog:
        """Audit entry for config operations (publish/rollback/revoke)."""
        return self.create(
            incident_id=None,
            actor=actor,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            details=details,
            source=source,
            request_id=request_id,
        )

    def create_discovery_audit(
        self,
        *,
        actor: str,
        action: str,
        resource_type: str,
        resource_id: str,
        details: dict[str, Any] | None = None,
        source: str = "worker",
        request_id: str | None = None,
    ) -> AuditLog:
        """Audit entry for discovery operations (auto_apply/reject)."""
        return self.create(
            incident_id=None,
            actor=actor,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            details=details,
            source=source,
            request_id=request_id,
        )

    def list_for_incident(self, incident_id: str) -> Sequence[AuditLog]:
        stmt = (
            select(AuditLog)
            .where(AuditLog.incident_id == incident_id)
            .order_by(AuditLog.created_at.desc())
        )
        return self.db.scalars(stmt).all()

    def list_recent(self, limit: int = 50) -> Sequence[AuditLog]:
        stmt = (
            select(AuditLog)
            .order_by(AuditLog.created_at.desc())
            .limit(limit)
        )
        return self.db.scalars(stmt).all()

    def query_by_action(self, action: str, limit: int = 100) -> Sequence[AuditLog]:
        stmt = (
            select(AuditLog)
            .where(AuditLog.action == action)
            .order_by(AuditLog.created_at.desc())
            .limit(limit)
        )
        return self.db.scalars(stmt).all()

    def query_by_target(
        self, resource_type: str, resource_id: str
    ) -> Sequence[AuditLog]:
        stmt = (
            select(AuditLog)
            .where(
                AuditLog.resource_type == resource_type,
                AuditLog.resource_id == resource_id,
            )
            .order_by(AuditLog.created_at.desc())
        )
        return self.db.scalars(stmt).all()

    def query_by_time_range(
        self,
        start: datetime,
        end: datetime,
        limit: int = 200,
    ) -> Sequence[AuditLog]:
        stmt = (
            select(AuditLog)
            .where(
                AuditLog.created_at >= start,
                AuditLog.created_at <= end,
            )
            .order_by(AuditLog.created_at.desc())
            .limit(limit)
        )
        return self.db.scalars(stmt).all()

    # No update() or delete() — audit logs are immutable by design.
    # Production should add a DB trigger to block raw UPDATE/DELETE.
