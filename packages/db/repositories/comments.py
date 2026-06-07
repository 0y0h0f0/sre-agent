"""Repository for incident_comments table — multi-person threaded comments."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from packages.common.ids import new_id
from packages.db.models import IncidentComment


class CommentRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def create(
        self,
        *,
        incident_id: str,
        author: str,
        content: str,
        parent_comment_id: str | None = None,
        mentioned_users: list[str] | None = None,
    ) -> IncidentComment:
        comment = IncidentComment(
            comment_id=new_id("cmt_"),
            incident_id=incident_id,
            author=author,
            content=content,
            parent_comment_id=parent_comment_id,
            mentioned_users=mentioned_users or [],
        )
        self.db.add(comment)
        return comment

    def list_for_incident(self, incident_id: str) -> Sequence[IncidentComment]:
        stmt = (
            select(IncidentComment)
            .where(IncidentComment.incident_id == incident_id)
            .order_by(IncidentComment.created_at.asc())
        )
        return self.db.scalars(stmt).all()

    def get_by_id(self, comment_id: str) -> IncidentComment | None:
        stmt = select(IncidentComment).where(IncidentComment.comment_id == comment_id)
        return self.db.scalar(stmt)

    def delete(self, comment_id: str) -> bool:
        comment = self.get_by_id(comment_id)
        if comment is None:
            return False
        self.db.delete(comment)
        return True
