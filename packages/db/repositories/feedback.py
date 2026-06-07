"""Repository for feedback_items table — user corrections to diagnosis."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from packages.common.ids import new_id
from packages.db.models import FeedbackItem


class FeedbackItemRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def create(
        self,
        *,
        incident_id: str,
        agent_run_id: str | None,
        feedback_type: str,
        original_value: dict[str, Any] | None,
        corrected_value: dict[str, Any] | None,
        delta: dict[str, Any] | None,
        submitted_by: str = "sre",
    ) -> FeedbackItem:
        feedback = FeedbackItem(
            feedback_id=new_id("fbk_"),
            incident_id=incident_id,
            agent_run_id=agent_run_id,
            feedback_type=feedback_type,
            original_value=original_value,
            corrected_value=corrected_value,
            delta=delta,
            submitted_by=submitted_by,
        )
        self.db.add(feedback)
        return feedback

    def list_for_incident(self, incident_id: str) -> Sequence[FeedbackItem]:
        stmt = (
            select(FeedbackItem)
            .where(FeedbackItem.incident_id == incident_id)
            .order_by(FeedbackItem.submitted_at.desc())
        )
        return self.db.scalars(stmt).all()

    def list_for_eval(
        self,
        *,
        feedback_type: str | None = None,
        limit: int = 100,
    ) -> Sequence[FeedbackItem]:
        stmt = select(FeedbackItem)
        if feedback_type is not None:
            stmt = stmt.where(FeedbackItem.feedback_type == feedback_type)
        else:
            stmt = stmt.where(
                FeedbackItem.feedback_type.in_(
                    ("root_cause_correction", "action_add", "action_remove")
                )
            )
        stmt = stmt.order_by(FeedbackItem.submitted_at.desc()).limit(limit)
        return self.db.scalars(stmt).all()

    def get_by_id(self, feedback_id: str) -> FeedbackItem | None:
        stmt = select(FeedbackItem).where(FeedbackItem.feedback_id == feedback_id)
        return self.db.scalar(stmt)
