"""Repository for evidence_items table."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from packages.common.ids import new_id
from packages.db.models import EvidenceItem


class EvidenceItemRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def create(
        self,
        *,
        incident_id: str,
        agent_run_id: str,
        type: str,
        source: str,
        source_id: str | None,
        title: str,
        excerpt: str,
        payload: dict[str, Any] | None = None,
        confidence: float | None = None,
    ) -> EvidenceItem:
        evidence = EvidenceItem(
            evidence_id=new_id("evi_"),
            incident_id=incident_id,
            agent_run_id=agent_run_id,
            type=type,
            source=source,
            source_id=source_id or "",
            title=title,
            excerpt=excerpt,
            payload=payload or {},
            confidence=confidence,
        )
        self.db.add(evidence)
        return evidence

    def list_for_incident(self, incident_id: str) -> Sequence[EvidenceItem]:
        stmt = (
            select(EvidenceItem)
            .where(EvidenceItem.incident_id == incident_id)
            .order_by(EvidenceItem.created_at.asc(), EvidenceItem.id.asc())
        )
        return self.db.scalars(stmt).all()

    def list_for_run(self, agent_run_id: str) -> Sequence[EvidenceItem]:
        stmt = (
            select(EvidenceItem)
            .where(EvidenceItem.agent_run_id == agent_run_id)
            .order_by(EvidenceItem.created_at.asc(), EvidenceItem.id.asc())
        )
        return self.db.scalars(stmt).all()
