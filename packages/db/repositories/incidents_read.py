from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from packages.db.models import Action, EvidenceItem


class IncidentReadRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def list_evidence(self, incident_id: str) -> Sequence[EvidenceItem]:
        stmt = (
            select(EvidenceItem)
            .where(EvidenceItem.incident_id == incident_id)
            .order_by(EvidenceItem.created_at.asc(), EvidenceItem.id.asc())
        )
        return self.db.scalars(stmt).all()

    def list_actions(self, incident_id: str) -> Sequence[Action]:
        stmt = (
            select(Action)
            .where(Action.incident_id == incident_id)
            .order_by(Action.created_at.asc(), Action.id.asc())
        )
        return self.db.scalars(stmt).all()
