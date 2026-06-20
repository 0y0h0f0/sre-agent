"""Repository for approvals table."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from packages.common.ids import new_id
from packages.common.time import utc_now
from packages.db.models import Action, Approval, Incident


class ApprovalRepository:
    """Data access for approval records and approval display queries."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def create(
        self,
        *,
        action_id: str,
        incident_id: str,
        agent_run_id: str,
        status: str = "waiting",
        risk_ack: bool = False,
        confirm_action_type: str | None = None,
        confirm_target: str | None = None,
    ) -> Approval:
        """Create a waiting approval for one action."""
        approval = Approval(
            approval_id=new_id("apv_"),
            action_id=action_id,
            incident_id=incident_id,
            agent_run_id=agent_run_id,
            status=status,
            risk_ack=risk_ack,
            confirm_action_type=confirm_action_type,
            confirm_target=confirm_target,
            requested_at=utc_now(),
        )
        self.db.add(approval)
        return approval

    def get_by_public_id(self, approval_id: str) -> Approval | None:
        stmt = select(Approval).where(Approval.approval_id == approval_id)
        return self.db.scalar(stmt)

    def get_for_update(self, approval_id: str) -> Approval | None:
        """SELECT ... FOR UPDATE to prevent TOCTOU race on approve/reject.

        ApprovalService checks status and updates the row in one transaction.
        The lock prevents two API requests from approving/rejecting the same
        approval concurrently.
        """
        stmt = (
            select(Approval)
            .where(Approval.approval_id == approval_id)
            .with_for_update()
        )
        return self.db.scalar(stmt)

    def list_for_incident(self, incident_id: str) -> Sequence[Approval]:
        stmt = (
            select(Approval)
            .where(Approval.incident_id == incident_id)
            .order_by(Approval.requested_at.desc(), Approval.id.desc())
        )
        return self.db.scalars(stmt).all()

    def list_waiting(self) -> Sequence[Approval]:
        """Return oldest waiting approvals, used by stale auto-approval jobs."""
        stmt = (
            select(Approval)
            .where(Approval.status == "waiting")
            .order_by(Approval.requested_at.asc(), Approval.id.asc())
        )
        return self.db.scalars(stmt).all()

    def list_for_run(self, agent_run_id: str) -> Sequence[Approval]:
        """Return all approvals for a given agent run, newest first."""
        stmt = (
            select(Approval)
            .where(Approval.agent_run_id == agent_run_id)
            .order_by(Approval.requested_at.desc(), Approval.id.desc())
        )
        return self.db.scalars(stmt).all()

    def has_waiting_for_run(self, agent_run_id: str) -> bool:
        """Whether the run still has any undecided approval in its current batch.

        Used to gate the LangGraph resume: a batch may contain several L2/L3
        approvals, and the graph applies them all in one resume. Resuming after
        only the first decision would execute the approved action and finalize
        the run, stranding the still-waiting siblings.
        """
        stmt = (
            select(func.count())
            .select_from(Approval)
            .where(
                Approval.agent_run_id == agent_run_id,
                Approval.status == "waiting",
            )
        )
        return int(self.db.scalar(stmt) or 0) > 0

    def get_display_item(self, approval_id: str) -> dict[str, Any] | None:
        """Return one approval joined with action and incident display fields."""
        stmt = (
            select(Approval, Action, Incident.service)
            .join(Action, Approval.action_id == Action.action_id)
            .join(Incident, Approval.incident_id == Incident.incident_id)
            .where(Approval.approval_id == approval_id)
        )
        row = self.db.execute(stmt).first()
        if row is None:
            return None
        approval, action, svc = row
        return self._display_item(approval, action, svc)

    def list_with_filters(
        self,
        *,
        status: str | None = None,
        incident_id: str | None = None,
        service: str | None = None,
        risk_level: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        """Return approvals joined with Action and Incident for display."""
        stmt = (
            select(Approval, Action, Incident.service)
            .join(Action, Approval.action_id == Action.action_id)
            .join(Incident, Approval.incident_id == Incident.incident_id)
        )
        count_stmt = (
            select(func.count())
            .select_from(Approval)
            .join(Action, Approval.action_id == Action.action_id)
            .join(Incident, Approval.incident_id == Incident.incident_id)
        )

        if status is not None:
            stmt = stmt.where(Approval.status == status)
            count_stmt = count_stmt.where(Approval.status == status)
        if incident_id is not None:
            stmt = stmt.where(Approval.incident_id == incident_id)
            count_stmt = count_stmt.where(Approval.incident_id == incident_id)
        if service is not None:
            stmt = stmt.where(Incident.service == service)
            count_stmt = count_stmt.where(Incident.service == service)
        if risk_level is not None:
            stmt = stmt.where(Action.risk_level == risk_level)
            count_stmt = count_stmt.where(Action.risk_level == risk_level)

        total = self.db.scalar(count_stmt) or 0
        stmt = (
            stmt.order_by(Approval.requested_at.desc(), Approval.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        rows = self.db.execute(stmt).all()

        items = [self._display_item(approval, action, svc) for approval, action, svc in rows]
        return items, total

    def _display_item(self, approval: Approval, action: Action, service: str) -> dict[str, Any]:
        """Normalize joined approval/action/incident columns for API schemas."""
        return {
            "approval_id": approval.approval_id,
            "action_id": action.action_id,
            "incident_id": approval.incident_id,
            "agent_run_id": approval.agent_run_id,
            "service": service,
            "action_type": action.type,
            "risk_level": action.risk_level,
            "approval_status": approval.status,
            "action_status": action.status,
            "reason": action.reason,
            "rollback_plan": action.rollback_plan,
            "requested_at": approval.requested_at,
            "decided_at": approval.decided_at,
            "approver": approval.approver,
            "comment": approval.comment,
        }

    def get_approved_for_action(self, action_id: str) -> Approval | None:
        """Find the latest approved approval for the given action."""
        stmt = (
            select(Approval)
            .where(Approval.action_id == action_id, Approval.status == "approved")
            .order_by(Approval.decided_at.desc(), Approval.id.desc())
        )
        return self.db.scalar(stmt)

    def update_decision(
        self,
        approval_id: str,
        *,
        status: str,
        approver: str,
        comment: str | None = None,
    ) -> Approval | None:
        """Set approval decision fields without committing."""
        approval = self.get_by_public_id(approval_id)
        if approval is None:
            return None
        approval.status = status
        approval.approver = approver
        approval.comment = comment
        approval.decided_at = utc_now()
        return approval

    def update_l3_confirmation(
        self,
        approval: Approval,
        *,
        risk_ack: bool,
        confirm_action_type: str,
        confirm_target: str,
    ) -> None:
        """Persist L3 secondary confirmation fields on the approval record.

        Manual action execution re-checks these fields against the action, so
        they remain durable proof of what the operator confirmed.
        """
        approval.risk_ack = risk_ack
        approval.confirm_action_type = confirm_action_type
        approval.confirm_target = confirm_target
