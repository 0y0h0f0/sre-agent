"""Business logic for incident comments and evidence annotations."""

from __future__ import annotations

from sqlalchemy.orm import Session

from apps.api.schemas.comments import (
    AnnotationCreate,
    AnnotationItem,
    AnnotationListResponse,
    CommentCreate,
    CommentItem,
    CommentListResponse,
)
from packages.common.errors import NotFoundError
from packages.db.repositories.audit_logs import AuditLogRepository
from packages.db.repositories.comments import CommentRepository
from packages.db.repositories.evidence_annotations import EvidenceAnnotationRepository
from packages.db.repositories.evidence_items import EvidenceItemRepository
from packages.db.repositories.incidents import IncidentRepository


class CommentService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.comments = CommentRepository(db)
        self.annotations = EvidenceAnnotationRepository(db)
        self.audit = AuditLogRepository(db)
        self.incidents = IncidentRepository(db)
        self.evidence_items = EvidenceItemRepository(db)

    # ---- Comments ----

    def create_comment(self, incident_id: str, data: CommentCreate) -> CommentItem:
        incident = self.incidents.get_by_public_id(incident_id)
        if incident is None:
            raise NotFoundError("incident", incident_id)

        comment = self.comments.create(
            incident_id=incident_id,
            author=data.author,
            content=data.content,
            parent_comment_id=data.parent_comment_id,
            mentioned_users=data.mentioned_users,
        )
        self.audit.create(
            incident_id=incident_id,
            actor=data.author,
            action="comment_add",
            resource_type="incident_comment",
            resource_id=comment.comment_id,
        )
        self.db.commit()
        return self._comment_item(comment)

    def list_comments(self, incident_id: str) -> CommentListResponse:
        items = self.comments.list_for_incident(incident_id)
        return CommentListResponse(
            items=[self._comment_item(c) for c in items],
            total=len(items),
        )

    def delete_comment(self, comment_id: str) -> None:
        if not self.comments.delete(comment_id):
            raise NotFoundError("comment", comment_id)
        self.db.commit()

    def _comment_item(self, comment) -> CommentItem:
        return CommentItem(
            comment_id=comment.comment_id,
            incident_id=comment.incident_id,
            author=comment.author,
            content=comment.content,
            parent_comment_id=comment.parent_comment_id,
            mentioned_users=comment.mentioned_users,
            created_at=comment.created_at,
        )

    # ---- Evidence Annotations ----

    def create_annotation(self, evidence_id: str, data: AnnotationCreate) -> AnnotationItem:
        ev = self.evidence_items.get_by_public_id(evidence_id)
        if ev is None:
            raise NotFoundError("evidence", evidence_id)

        annotation = self.annotations.create(
            evidence_id=evidence_id,
            incident_id=ev.incident_id,
            author=data.author,
            content=data.content,
        )
        self.audit.create(
            incident_id=ev.incident_id,
            actor=data.author,
            action="evidence_annotate",
            resource_type="evidence_annotation",
            resource_id=annotation.annotation_id,
        )
        self.db.commit()
        return self._annotation_item(annotation)

    def list_annotations(self, evidence_id: str) -> AnnotationListResponse:
        items = self.annotations.list_for_evidence(evidence_id)
        return AnnotationListResponse(
            items=[self._annotation_item(a) for a in items],
            total=len(items),
        )

    def _annotation_item(self, annotation) -> AnnotationItem:
        return AnnotationItem(
            annotation_id=annotation.annotation_id,
            evidence_id=annotation.evidence_id,
            incident_id=annotation.incident_id,
            author=annotation.author,
            content=annotation.content,
            created_at=annotation.created_at,
        )
