"""Repository for evidence_annotations table."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from packages.common.ids import new_id
from packages.db.models import EvidenceAnnotation


class EvidenceAnnotationRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def create(
        self,
        *,
        evidence_id: str,
        incident_id: str,
        author: str,
        content: str,
    ) -> EvidenceAnnotation:
        annotation = EvidenceAnnotation(
            annotation_id=new_id("ean_"),
            evidence_id=evidence_id,
            incident_id=incident_id,
            author=author,
            content=content,
        )
        self.db.add(annotation)
        return annotation

    def list_for_evidence(self, evidence_id: str) -> Sequence[EvidenceAnnotation]:
        stmt = (
            select(EvidenceAnnotation)
            .where(EvidenceAnnotation.evidence_id == evidence_id)
            .order_by(EvidenceAnnotation.created_at.asc())
        )
        return self.db.scalars(stmt).all()

    def list_for_incident(self, incident_id: str) -> Sequence[EvidenceAnnotation]:
        stmt = (
            select(EvidenceAnnotation)
            .where(EvidenceAnnotation.incident_id == incident_id)
            .order_by(EvidenceAnnotation.created_at.asc())
        )
        return self.db.scalars(stmt).all()

    def get_by_id(self, annotation_id: str) -> EvidenceAnnotation | None:
        stmt = select(EvidenceAnnotation).where(EvidenceAnnotation.annotation_id == annotation_id)
        return self.db.scalar(stmt)

    def delete(self, annotation_id: str) -> bool:
        annotation = self.get_by_id(annotation_id)
        if annotation is None:
            return False
        self.db.delete(annotation)
        return True
