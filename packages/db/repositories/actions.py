"""Repository for actions table."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from packages.common.ids import new_id
from packages.db.models import Action


class ActionRepository:
    """Data access for action rows created by graph/API execution paths."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def create(
        self,
        *,
        incident_id: str,
        agent_run_id: str,
        type: str,
        risk_level: str,
        status: str,
        executor: str = "mock",
        target: str | None = None,
        params: dict[str, Any] | None = None,
        reason: str | None = None,
        rollback_plan: str | None = None,
    ) -> Action:
        """Create an action row for a planned or executable action.

        Approval-gated actions are created before interrupt; L0/L1 automatic
        actions may be created immediately before execution.
        """
        action = Action(
            action_id=new_id("act_"),
            incident_id=incident_id,
            agent_run_id=agent_run_id,
            type=type,
            risk_level=risk_level,
            status=status,
            executor=executor,
            target=target or "",
            params=params or {},
            reason=reason or "",
            rollback_plan=rollback_plan or "",
        )
        self.db.add(action)
        return action

    def get_by_public_id(self, action_id: str) -> Action | None:
        stmt = select(Action).where(Action.action_id == action_id)
        return self.db.scalar(stmt)

    def list_for_incident(self, incident_id: str) -> Sequence[Action]:
        """Return actions in creation order for incident detail/reporting."""
        stmt = (
            select(Action)
            .where(Action.incident_id == incident_id)
            .order_by(Action.created_at.asc(), Action.id.asc())
        )
        return self.db.scalars(stmt).all()

    def list_for_run(self, agent_run_id: str) -> Sequence[Action]:
        """Return actions created by a single agent run."""
        stmt = (
            select(Action)
            .where(Action.agent_run_id == agent_run_id)
            .order_by(Action.created_at.asc(), Action.id.asc())
        )
        return self.db.scalars(stmt).all()

    def update_status(
        self,
        action_id: str,
        status: str,
        execution_result: dict[str, Any] | None = None,
    ) -> Action | None:
        """Update action status and optional executor result without commit."""
        action = self.get_by_public_id(action_id)
        if action is None:
            return None
        action.status = status
        if execution_result is not None:
            action.execution_result = execution_result
        return action
