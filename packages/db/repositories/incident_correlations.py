"""Repository for incident_correlations table — cross-incident associations."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from packages.common.ids import new_id
from packages.db.models import Incident, IncidentCorrelation


class IncidentCorrelationRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def create(
        self,
        incident_id_a: str,
        incident_id_b: str,
        correlation_type: str,
        similarity_score: float | None = None,
    ) -> IncidentCorrelation:
        existing = self._find_existing(incident_id_a, incident_id_b)
        if existing is not None:
            return existing

        correlation = IncidentCorrelation(
            correlation_id=new_id("cor_"),
            incident_id_a=incident_id_a,
            incident_id_b=incident_id_b,
            correlation_type=correlation_type,
            similarity_score=similarity_score,
        )
        self.db.add(correlation)
        try:
            self.db.flush()
        except IntegrityError:
            self.db.rollback()
            existing = self._find_existing(incident_id_a, incident_id_b)
            if existing is not None:
                return existing
            raise
        return correlation

    def get_for_incident(self, incident_id: str) -> Sequence[IncidentCorrelation]:
        stmt = (
            select(IncidentCorrelation)
            .where(
                (IncidentCorrelation.incident_id_a == incident_id)
                | (IncidentCorrelation.incident_id_b == incident_id)
            )
            .order_by(IncidentCorrelation.created_at.desc())
        )
        return self.db.scalars(stmt).all()

    def find_by_fingerprint(
        self, fingerprint: str, *, exclude_incident_id: str | None = None, limit: int = 10
    ) -> Sequence[Incident]:
        stmt = select(Incident).where(Incident.fingerprint == fingerprint)
        if exclude_incident_id is not None:
            stmt = stmt.where(Incident.incident_id != exclude_incident_id)
        stmt = stmt.order_by(Incident.created_at.desc()).limit(limit)
        return self.db.scalars(stmt).all()

    def find_similar_by_service(
        self, service: str, *, exclude_incident_id: str | None = None, limit: int = 10
    ) -> Sequence[Incident]:
        stmt = select(Incident).where(Incident.service == service)
        if exclude_incident_id is not None:
            stmt = stmt.where(Incident.incident_id != exclude_incident_id)
        stmt = stmt.order_by(Incident.created_at.desc()).limit(limit)
        return self.db.scalars(stmt).all()

    def _find_existing(
        self, incident_id_a: str, incident_id_b: str
    ) -> IncidentCorrelation | None:
        stmt = select(IncidentCorrelation).where(
            (
                (IncidentCorrelation.incident_id_a == incident_id_a)
                & (IncidentCorrelation.incident_id_b == incident_id_b)
            )
            | (
                (IncidentCorrelation.incident_id_a == incident_id_b)
                & (IncidentCorrelation.incident_id_b == incident_id_a)
            )
        )
        return self.db.scalar(stmt)
