from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from apps.api.schemas.alerts import AlertCreateRequest
from apps.api.schemas.common import IncidentStatus
from packages.common.time import ensure_utc
from packages.db.models import Incident

TERMINAL_INCIDENT_STATUSES = (
    IncidentStatus.RESOLVED.value,
    IncidentStatus.FAILED.value,
    IncidentStatus.MITIGATED.value,
)


class IncidentRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def create(self, incident_id: str, payload: AlertCreateRequest) -> Incident:
        incident = Incident(
            incident_id=incident_id,
            fingerprint=payload.fingerprint,
            source=payload.source,
            service=payload.service,
            severity=payload.severity.value,
            alert_name=payload.alert_name,
            status=IncidentStatus.OPEN.value,
            starts_at=ensure_utc(payload.starts_at),
            ends_at=ensure_utc(payload.ends_at) if payload.ends_at else None,
            labels=dict(payload.labels),
            annotations=dict(payload.annotations),
            raw_payload=dict(payload.raw_payload),
        )
        self.db.add(incident)
        return incident

    def get_by_public_id(self, incident_id: str) -> Incident | None:
        stmt = select(Incident).where(Incident.incident_id == incident_id)
        return self.db.scalar(stmt)

    def get_open_by_fingerprint(self, fingerprint: str) -> Incident | None:
        stmt = select(Incident).where(
            Incident.fingerprint == fingerprint,
            Incident.status.not_in(TERMINAL_INCIDENT_STATUSES),
        )
        return self.db.scalar(stmt)

    def _base_query(
        self, *, status: str | None, service: str | None, severity: str | None
    ) -> Select[tuple[Incident]]:
        stmt: Select[tuple[Incident]] = select(Incident)
        if status:
            stmt = stmt.where(Incident.status == status)
        if service:
            stmt = stmt.where(Incident.service == service)
        if severity:
            stmt = stmt.where(Incident.severity == severity)
        return stmt

    def list_all(self, *, limit: int = 1000) -> Sequence[Incident]:
        stmt = select(Incident).order_by(Incident.created_at.desc()).limit(limit)
        return self.db.scalars(stmt).all()

    def list(
        self,
        *,
        status: str | None = None,
        service: str | None = None,
        severity: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> Sequence[Incident]:
        stmt = self._base_query(status=status, service=service, severity=severity)
        offset = (page - 1) * page_size
        stmt = stmt.order_by(Incident.created_at.desc()).offset(offset).limit(page_size)
        return self.db.scalars(stmt).all()

    def list_with_count(
        self,
        *,
        status: str | None = None,
        service: str | None = None,
        severity: str | None = None,
        page: int = 1,
        page_size: int = 20,
        cursor: str | None = None,
    ) -> tuple[Sequence[Incident], int]:
        """List incidents with total count. Supports both OFFSET and cursor-based pagination.

        When ``cursor`` is provided, uses cursor-based pagination (``WHERE created_at < :cursor``)
        which scales better for large tables than OFFSET.
        """
        base = self._base_query(status=status, service=service, severity=severity)
        count_stmt = select(func.count()).select_from(base.subquery())
        total: int = self.db.scalar(count_stmt) or 0
        if cursor:
            items_stmt = (
                base.order_by(Incident.created_at.desc())
                .where(Incident.created_at < cursor)
                .limit(page_size)
            )
        else:
            offset = (page - 1) * page_size
            items_stmt = base.order_by(Incident.created_at.desc()).offset(offset).limit(page_size)
        items = self.db.scalars(items_stmt).all()
        return items, total

    def alert_payload(self, incident: Incident) -> dict[str, Any]:
        return {
            "source": incident.source,
            "fingerprint": incident.fingerprint,
            "service": incident.service,
            "severity": incident.severity,
            "alert_name": incident.alert_name,
            "starts_at": incident.starts_at,
            "ends_at": incident.ends_at,
            "labels": incident.labels,
            "annotations": incident.annotations,
            "raw_payload": incident.raw_payload,
        }
