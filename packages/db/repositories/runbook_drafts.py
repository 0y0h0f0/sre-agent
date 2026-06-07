"""Repository for RunbookDraft CRUD operations."""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from packages.common.ids import new_id
from packages.common.time import utc_now
from packages.db.models import RunbookDraft


class RunbookDraftRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def create(
        self,
        *,
        fingerprint: str,
        incident_ids: list[str],
        service: str,
        incident_type: str,
        title: str,
        content: str,
        front_matter: dict[str, Any],
        source_chunk_ids: list[str] | None = None,
        llm_model: str | None = None,
    ) -> RunbookDraft:
        draft = RunbookDraft(
            draft_id=new_id("drf_"),
            fingerprint=fingerprint,
            incident_ids=incident_ids,
            service=service,
            incident_type=incident_type,
            title=title,
            content=content,
            front_matter=front_matter,
            status="draft",
            source_chunk_ids=source_chunk_ids,
            llm_model=llm_model,
        )
        self.db.add(draft)
        self.db.flush()
        return draft

    def get_by_draft_id(self, draft_id: str) -> RunbookDraft | None:
        stmt = select(RunbookDraft).where(RunbookDraft.draft_id == draft_id)
        return self.db.scalar(stmt)

    def list_by_status(self, status: str) -> list[RunbookDraft]:
        stmt = (
            select(RunbookDraft)
            .where(RunbookDraft.status == status)
            .order_by(RunbookDraft.created_at.desc())
        )
        return list(self.db.scalars(stmt).all())

    def list_by_service(self, service: str) -> list[RunbookDraft]:
        stmt = (
            select(RunbookDraft)
            .where(RunbookDraft.service == service)
            .order_by(RunbookDraft.created_at.desc())
        )
        return list(self.db.scalars(stmt).all())

    def list_by_fingerprint(self, fingerprint: str) -> list[RunbookDraft]:
        stmt = (
            select(RunbookDraft)
            .where(RunbookDraft.fingerprint == fingerprint)
            .order_by(RunbookDraft.created_at.desc())
        )
        return list(self.db.scalars(stmt).all())

    def has_draft_for_fingerprint(self, fingerprint: str) -> bool:
        stmt = (
            select(RunbookDraft.id)
            .where(RunbookDraft.fingerprint == fingerprint)
            .where(RunbookDraft.status.in_(["draft", "published"]))
            .limit(1)
        )
        return self.db.scalar(stmt) is not None

    def update_status(
        self,
        draft_id: str,
        status: str,
        *,
        reviewer: str | None = None,
        comment: str | None = None,
    ) -> RunbookDraft | None:
        draft = self.get_by_draft_id(draft_id)
        if draft is None:
            return None
        draft.status = status
        draft.reviewer = reviewer
        draft.review_comment = comment
        draft.updated_at = utc_now()
        return draft

    def list_all(
        self, *, status: str | None = None, service: str | None = None
    ) -> list[RunbookDraft]:
        stmt = select(RunbookDraft)
        if status:
            stmt = stmt.where(RunbookDraft.status == status)
        if service:
            stmt = stmt.where(RunbookDraft.service == service)
        stmt = stmt.order_by(RunbookDraft.created_at.desc())
        return list(self.db.scalars(stmt).all())

    def count_by_status(self, status: str) -> int:
        stmt = (
            select(func.count())
            .select_from(RunbookDraft)
            .where(RunbookDraft.status == status)
        )
        return int(self.db.scalar(stmt) or 0)
