"""Business logic for approval list, approve, and reject."""

from __future__ import annotations

from collections.abc import Callable

from sqlalchemy.orm import Session

from apps.api.schemas.approvals import (
    ApprovalDecisionResponse,
    ApprovalItem,
    ApproveRequest,
    RejectRequest,
)
from apps.api.schemas.common import (
    ActionStatus,
    ApprovalStatus,
    PaginatedResponse,
    RiskLevel,
)
from packages.common.errors import (
    ConflictError,
    NotFoundError,
    ValidationAppError,
)
from packages.db.models import Action, Approval
from packages.db.repositories.actions import ActionRepository
from packages.db.repositories.approvals import ApprovalRepository

TaskEnqueue = Callable[[str, str], str]


class ApprovalService:
    def __init__(
        self,
        db: Session,
        enqueue_resume: TaskEnqueue | None = None,
    ) -> None:
        self.db = db
        self.enqueue_resume = enqueue_resume
        self.approvals = ApprovalRepository(db)
        self.actions = ActionRepository(db)

    def list_approvals(
        self,
        *,
        status: str | None = None,
        incident_id: str | None = None,
        service: str | None = None,
        risk_level: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> PaginatedResponse:
        items, total = self.approvals.list_with_filters(
            status=status,
            incident_id=incident_id,
            service=service,
            risk_level=risk_level,
            page=page,
            page_size=page_size,
        )
        return PaginatedResponse(
            items=[
                ApprovalItem(
                    approval_id=item["approval_id"],
                    action_id=item["action_id"],
                    incident_id=item["incident_id"],
                    agent_run_id=item["agent_run_id"],
                    service=item["service"],
                    action_type=item["action_type"],
                    risk_level=RiskLevel(item["risk_level"]),
                    approval_status=ApprovalStatus(item["approval_status"]),
                    action_status=ActionStatus(item["action_status"]),
                    reason=item["reason"],
                    rollback_plan=item.get("rollback_plan"),
                    requested_at=item["requested_at"],
                    decided_at=item.get("decided_at"),
                    approver=item.get("approver"),
                    comment=item.get("comment"),
                )
                for item in items
            ],
            total=total,
            page=page,
            page_size=page_size,
        )

    def list_for_incident(self, incident_id: str) -> list[ApprovalItem]:
        items, _ = self.approvals.list_with_filters(incident_id=incident_id, page_size=500)
        return [
            ApprovalItem(
                approval_id=item["approval_id"],
                action_id=item["action_id"],
                incident_id=item["incident_id"],
                agent_run_id=item["agent_run_id"],
                service=item["service"],
                action_type=item["action_type"],
                risk_level=RiskLevel(item["risk_level"]),
                approval_status=ApprovalStatus(item["approval_status"]),
                action_status=ActionStatus(item["action_status"]),
                reason=item["reason"],
                rollback_plan=item.get("rollback_plan"),
                requested_at=item["requested_at"],
                decided_at=item.get("decided_at"),
                approver=item.get("approver"),
                comment=item.get("comment"),
            )
            for item in items
        ]

    def approve(self, approval_id: str, request: ApproveRequest) -> ApprovalDecisionResponse:
        approval = self._require_approval(approval_id)
        if approval.status != ApprovalStatus.WAITING.value:
            raise ConflictError(
                "approval has already been decided",
                details={"approval_id": approval_id, "current_status": approval.status},
            )

        action = self._require_action(approval.action_id)

        # L3 validation: must provide secondary confirmation
        if action.risk_level == "L3":
            if not request.risk_ack:
                raise ValidationAppError(
                    "L3 actions require risk_ack=true",
                    details={"missing_field": "risk_ack"},
                )
            if not request.confirm_action_type:
                raise ValidationAppError(
                    "L3 actions require confirm_action_type",
                    details={"missing_field": "confirm_action_type"},
                )
            if request.confirm_action_type != action.type:
                raise ValidationAppError(
                    f"confirm_action_type mismatch: expected '{action.type}', "
                    f"got '{request.confirm_action_type}'",
                    details={
                        "expected": action.type,
                        "provided": request.confirm_action_type,
                    },
                )
            if not request.confirm_target:
                raise ValidationAppError(
                    "L3 actions require confirm_target",
                    details={"missing_field": "confirm_target"},
                )
            if request.confirm_target != (action.target or ""):
                raise ValidationAppError(
                    f"confirm_target mismatch: expected '{(action.target or '')}', "
                    f"got '{request.confirm_target}'",
                    details={
                        "expected": action.target or "",
                        "provided": request.confirm_target,
                    },
                )

            # Persist L3 confirmation fields
            self.approvals.update_l3_confirmation(
                approval,
                risk_ack=request.risk_ack,
                confirm_action_type=request.confirm_action_type,
                confirm_target=request.confirm_target,
            )

        # Update approval and action statuses
        self.approvals.update_decision(
            approval_id,
            status=ApprovalStatus.APPROVED.value,
            approver=request.approver,
            comment=request.comment,
        )
        self.actions.update_status(action.action_id, ActionStatus.APPROVED.value)
        self.db.flush()

        # Resume only once the whole batch is decided. Resuming after the first
        # decision would execute the approved action and finalize the run,
        # leaving sibling approvals stranded (approved-but-never-executed).
        self._maybe_resume(approval.agent_run_id, "approved")

        return ApprovalDecisionResponse(
            approval_id=approval.approval_id,
            action_id=approval.action_id,
            status=ApprovalStatus.APPROVED,
            agent_run_id=approval.agent_run_id,
        )

    def reject(self, approval_id: str, request: RejectRequest) -> ApprovalDecisionResponse:
        approval = self._require_approval(approval_id)
        if approval.status != ApprovalStatus.WAITING.value:
            raise ConflictError(
                "approval has already been decided",
                details={"approval_id": approval_id, "current_status": approval.status},
            )

        self.approvals.update_decision(
            approval_id,
            status=ApprovalStatus.REJECTED.value,
            approver=request.approver,
            comment=request.comment,
        )
        self.actions.update_status(approval.action_id, ActionStatus.REJECTED.value)
        self.db.flush()

        # Resume only once the whole batch is decided (see approve()).
        self._maybe_resume(approval.agent_run_id, "rejected")

        return ApprovalDecisionResponse(
            approval_id=approval.approval_id,
            action_id=approval.action_id,
            status=ApprovalStatus.REJECTED,
            agent_run_id=approval.agent_run_id,
        )

    def _maybe_resume(self, agent_run_id: str, decision: str) -> None:
        """Enqueue a resume only when no approval in the run is still waiting."""
        if self.enqueue_resume is None:
            return
        if self.approvals.has_waiting_for_run(agent_run_id):
            return
        self.enqueue_resume(agent_run_id, decision)

    def _require_approval(self, approval_id: str) -> Approval:
        approval = self.approvals.get_by_public_id(approval_id)
        if approval is None:
            raise NotFoundError("approval", approval_id)
        return approval

    def _require_action(self, action_id: str) -> Action:
        action = self.actions.get_by_public_id(action_id)
        if action is None:
            raise NotFoundError("action", action_id)
        return action
