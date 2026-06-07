"""Repository for audit_logs table — write-ahead operation audit."""

from __future__ import annotations

from collections.abc import Sequence
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
    ) -> AuditLog:
        audit = AuditLog(
            audit_id=new_id("adt_"),
            incident_id=incident_id,
            actor=actor,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            details=details or {},
        )
        self.db.add(audit)
        return audit

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
