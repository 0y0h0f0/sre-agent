"""Business logic for approval list, approve, and reject."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sqlalchemy.orm import Session

from apps.api.schemas.approvals import (
    ApprovalDecisionResponse,
    ApprovalItem,
    ApproveRequest,
    BatchApprovalRequest,
    RejectRequest,
    TokenApprovalRequest,
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
from packages.db.repositories.audit_logs import AuditLogRepository

TaskEnqueue = Callable[[str, str], str]


class ApprovalService:
    """Owns approval decisions and LangGraph resume enqueue boundaries."""

    def __init__(
        self,
        db: Session,
        enqueue_resume: TaskEnqueue | None = None,
    ) -> None:
        self.db = db
        self.enqueue_resume = enqueue_resume
        self.approvals = ApprovalRepository(db)
        self.actions = ActionRepository(db)
        self.audit = AuditLogRepository(db)

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
            items=[self._approval_item(item) for item in items],
            total=total,
            page=page,
            page_size=page_size,
        )

    def get_approval(self, approval_id: str) -> ApprovalItem:
        item = self.approvals.get_display_item(approval_id)
        if item is None:
            raise NotFoundError("approval", approval_id)
        return self._approval_item(item)

    def list_for_incident(self, incident_id: str) -> list[ApprovalItem]:
        items, _ = self.approvals.list_with_filters(incident_id=incident_id, page_size=500)
        return [self._approval_item(item) for item in items]

    def _approval_item(self, item: dict[str, Any]) -> ApprovalItem:
        return ApprovalItem(
            approval_id=str(item["approval_id"]),
            action_id=str(item["action_id"]),
            incident_id=str(item["incident_id"]),
            agent_run_id=str(item["agent_run_id"]),
            service=str(item["service"]),
            action_type=str(item["action_type"]),
            risk_level=RiskLevel(str(item["risk_level"])),
            approval_status=ApprovalStatus(str(item["approval_status"])),
            action_status=ActionStatus(str(item["action_status"])),
            reason=str(item["reason"]),
            rollback_plan=item.get("rollback_plan"),
            requested_at=item["requested_at"],
            decided_at=item.get("decided_at"),
            approver=item.get("approver"),
            comment=item.get("comment"),
        )

    def approve(self, approval_id: str, request: ApproveRequest) -> ApprovalDecisionResponse:
        """Approve one waiting approval and enqueue resume when the batch is done."""
        approval = self._require_approval(approval_id)
        if approval.status != ApprovalStatus.WAITING.value:
            raise ConflictError(
                "approval has already been decided",
                details={"approval_id": approval_id, "current_status": approval.status},
            )

        action = self._require_action(approval.action_id)

        # L3 validation: must provide secondary confirmation. This duplicates
        # the UI requirement at the service boundary so API clients cannot
        # bypass the second-confirm contract.
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

            # Persist L3 confirmation fields alongside the approval row so later
            # manual action execution can re-check exactly what was confirmed.
            self.approvals.update_l3_confirmation(
                approval,
                risk_ack=request.risk_ack,
                confirm_action_type=request.confirm_action_type,
                confirm_target=request.confirm_target,
            )

        # Update approval and action statuses in the same transaction as audit
        # creation, then commit before resume enqueue.
        self.approvals.update_decision(
            approval_id,
            status=ApprovalStatus.APPROVED.value,
            approver=request.approver,
            comment=request.comment,
        )
        self.actions.update_status(action.action_id, ActionStatus.APPROVED.value)
        self.db.flush()

        self.audit.create(
            incident_id=approval.incident_id,
            actor=request.approver,
            action="approve",
            resource_type="approval",
            resource_id=approval.approval_id,
            details={"action_id": action.action_id, "risk_level": action.risk_level},
        )
        self.db.flush()

        # Persist the decision before enqueuing the resume so the Celery
        # worker (separate connection) can read the updated approval status.
        self.db.commit()

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
        """Reject one waiting approval and enqueue resume when the batch is done."""
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

        action = self._require_action(approval.action_id)
        self.audit.create(
            incident_id=approval.incident_id,
            actor=request.approver,
            action="reject",
            resource_type="approval",
            resource_id=approval.approval_id,
            details={"action_id": approval.action_id, "risk_level": action.risk_level},
        )
        self.db.flush()

        # Persist the decision before enqueuing the resume so the Celery
        # worker (separate connection) can read the updated approval status.
        self.db.commit()

        # Resume only once the whole batch is decided (see approve()).
        self._maybe_resume(approval.agent_run_id, "rejected")

        return ApprovalDecisionResponse(
            approval_id=approval.approval_id,
            action_id=approval.action_id,
            status=ApprovalStatus.REJECTED,
            agent_run_id=approval.agent_run_id,
        )

    def batch_decide(self, request: BatchApprovalRequest) -> list[ApprovalDecisionResponse]:
        """Process multiple approvals atomically.

        The batch is preflighted before any row is updated. This avoids the
        unsafe partial-success case where an L2 action could be approved while
        a sibling L3 approval in the same request failed confirmation.
        """
        pairs = self._preflight_batch(request)
        results: list[ApprovalDecisionResponse] = []
        impacted_runs: dict[str, str] = {}
        approved = request.decision == "approve"
        approval_status = (
            ApprovalStatus.APPROVED.value if approved else ApprovalStatus.REJECTED.value
        )
        audit_action = "approve" if approved else "reject"
        resume_decision = "approved" if approved else "rejected"

        for approval, action in pairs:
            if approved and action.risk_level == "L3":
                self.approvals.update_l3_confirmation(
                    approval,
                    risk_ack=request.risk_ack,
                    confirm_action_type=request.confirm_action_type or "",
                    confirm_target=request.confirm_target or "",
                )

            self.approvals.update_decision(
                approval.approval_id,
                status=approval_status,
                approver=request.approver,
                comment=request.comment,
            )
            self.actions.update_status(action.action_id, approval_status)
            self.audit.create(
                incident_id=approval.incident_id,
                actor=request.approver,
                action=audit_action,
                resource_type="approval",
                resource_id=approval.approval_id,
                details={"action_id": action.action_id, "risk_level": action.risk_level},
            )
            # A batch can span multiple runs. Resume each impacted run once all
            # DB rows are committed and no approvals remain waiting for that run.
            impacted_runs[approval.agent_run_id] = resume_decision
            results.append(
                ApprovalDecisionResponse(
                    approval_id=approval.approval_id,
                    action_id=approval.action_id,
                    status=ApprovalStatus(approval_status),
                    agent_run_id=approval.agent_run_id,
                )
            )

        self.db.flush()
        self.db.commit()

        for agent_run_id, decision in impacted_runs.items():
            self._maybe_resume(agent_run_id, decision)

        return results

    def _preflight_batch(self, request: BatchApprovalRequest) -> list[tuple[Approval, Action]]:
        """Validate a batch completely before mutating any approval rows."""
        seen: set[str] = set()
        duplicates: list[str] = []
        for approval_id in request.approval_ids:
            if approval_id in seen:
                duplicates.append(approval_id)
            seen.add(approval_id)
        if duplicates:
            raise ValidationAppError(
                "batch approval_ids must be unique",
                details={"duplicates": sorted(set(duplicates))},
            )

        pairs: list[tuple[Approval, Action]] = []
        errors: list[dict[str, Any]] = []
        l3_approval_ids: list[str] = []
        for approval_id in request.approval_ids:
            approval = (
                self.approvals.get_for_update(approval_id)
                or self.approvals.get_by_public_id(approval_id)
            )
            if approval is None:
                errors.append({"approval_id": approval_id, "error": "approval not found"})
                continue
            if approval.status != ApprovalStatus.WAITING.value:
                errors.append(
                    {
                        "approval_id": approval_id,
                        "error": "approval has already been decided",
                        "current_status": approval.status,
                    }
                )
                continue
            action = self.actions.get_by_public_id(approval.action_id)
            if action is None:
                errors.append(
                    {
                        "approval_id": approval_id,
                        "action_id": approval.action_id,
                        "error": "action not found",
                    }
                )
                continue
            if request.decision == "approve" and action.risk_level == "L3":
                l3_approval_ids.append(approval.approval_id)
                missing_or_mismatch = self._l3_confirmation_errors(action, request)
                if missing_or_mismatch:
                    errors.append(
                        {
                            "approval_id": approval_id,
                            "action_id": action.action_id,
                            "risk_level": action.risk_level,
                            "errors": missing_or_mismatch,
                        }
                    )
                    continue
            pairs.append((approval, action))

        if request.decision == "approve" and len(l3_approval_ids) > 1:
            # One request body has one confirm_action_type/confirm_target pair,
            # so it cannot safely confirm multiple distinct L3 actions.
            errors.append(
                {
                    "approval_ids": l3_approval_ids,
                    "risk_level": "L3",
                    "error": "batch approve supports at most one L3 approval",
                }
            )

        if errors:
            raise ValidationAppError(
                "batch approvals validation failed",
                details={"errors": errors},
            )

        return pairs

    @staticmethod
    def _l3_confirmation_errors(
        action: Action,
        request: BatchApprovalRequest,
    ) -> list[dict[str, str]]:
        errors: list[dict[str, str]] = []
        if not request.risk_ack:
            errors.append({"field": "risk_ack", "error": "required_true"})
        if not request.confirm_action_type:
            errors.append({"field": "confirm_action_type", "error": "required"})
        elif request.confirm_action_type != action.type:
            errors.append(
                {
                    "field": "confirm_action_type",
                    "error": "mismatch",
                    "expected": action.type,
                    "provided": request.confirm_action_type,
                }
            )
        expected_target = action.target or ""
        if not request.confirm_target:
            errors.append({"field": "confirm_target", "error": "required"})
        elif request.confirm_target != expected_target:
            errors.append(
                {
                    "field": "confirm_target",
                    "error": "mismatch",
                    "expected": expected_target,
                    "provided": request.confirm_target,
                }
            )
        return errors

    def generate_email_token(self, approval_id: str) -> str:
        """Generate a single-use email token for an approval. Expires in 24h."""
        import secrets
        from datetime import timedelta

        from packages.common.time import utc_now

        approval = self._require_approval(approval_id)
        # Store only the generated token on the approval row; callers receive it
        # once for email link construction.
        token = secrets.token_urlsafe(24)
        approval.email_token = token
        approval.email_token_expires_at = utc_now() + timedelta(hours=24)
        self.db.flush()
        self.db.commit()
        return token

    def get_approval_by_token(self, token: str) -> ApprovalItem:
        """Look up an approval by its email token."""
        from sqlalchemy import select

        stmt = select(Approval).where(Approval.email_token == token)
        approval = self.db.scalar(stmt)
        if approval is None:
            raise NotFoundError("approval", f"token:{token[:8]}...")
        return self.get_approval(approval.approval_id)

    def approve_by_token(
        self, token: str, request: TokenApprovalRequest
    ) -> ApprovalDecisionResponse:
        """Approve via email token. L3 actions require web UI — rejected here."""
        approval = self._get_approval_by_token(token)
        action = self._require_action(approval.action_id)

        # L3 requires full confirmation in web UI — not available via email.
        # Rejecting via token is allowed because it never increases risk.
        if action.risk_level == "L3":
            raise ValidationAppError(
                "L3 actions require web UI confirmation and cannot be approved via email link. "
                "Please use the web console for risk_ack, action_type, and target confirmation.",
                details={"approval_id": approval.approval_id, "risk_level": "L3"},
            )

        approve_req = ApproveRequest(
            approver=request.approver,
            comment=request.comment,
        )
        result = self.approve(approval.approval_id, approve_req)
        # Consume token (approve() already committed, so commit the token clear)
        approval.email_token = None
        self.db.flush()
        self.db.commit()
        return result

    def reject_by_token(
        self, token: str, request: TokenApprovalRequest
    ) -> ApprovalDecisionResponse:
        """Reject via email token."""
        approval = self._get_approval_by_token(token)
        reject_req = RejectRequest(
            approver=request.approver,
            comment=request.comment,
        )
        result = self.reject(approval.approval_id, reject_req)
        # Consume token (reject() already committed, so commit the token clear)
        approval.email_token = None
        self.db.flush()
        self.db.commit()
        return result

    def _get_approval_by_token(self, token: str) -> Approval:
        """Load a non-expired email-token approval or clear expired tokens."""
        from sqlalchemy import select

        from packages.common.time import utc_now

        stmt = select(Approval).where(Approval.email_token == token)
        approval = self.db.scalar(stmt)
        if approval is None:
            raise NotFoundError("approval", f"token:{token[:8]}...")

        # Check expiry
        if (
            approval.email_token_expires_at is not None
            and approval.email_token_expires_at < utc_now()
        ):
            # Clear expired token so repeated use fails consistently and old
            # links cannot later become valid again.
            approval.email_token = None
            approval.email_token_expires_at = None
            self.db.flush()
            self.db.commit()
            raise ValidationAppError(
                "email approval token has expired",
                details={"approval_id": approval.approval_id},
            )
        return approval

    def _maybe_resume(self, agent_run_id: str, decision: str) -> None:
        """Enqueue a resume only when no approval in the run is still waiting."""
        if self.enqueue_resume is None:
            import logging
            logging.getLogger(__name__).warning(
                "cannot enqueue resume for run %s: enqueue_resume not configured", agent_run_id
            )
            return
        if self.approvals.has_waiting_for_run(agent_run_id):
            # LangGraph resume should see a complete batch decision; otherwise
            # it could execute approved siblings while waiting siblings remain.
            return
        self.enqueue_resume(agent_run_id, decision)

    def _require_approval(self, approval_id: str) -> Approval:
        # Use FOR UPDATE to prevent TOCTOU race between status check and update
        approval = (
            self.approvals.get_for_update(approval_id)
            or self.approvals.get_by_public_id(approval_id)
        )
        if approval is None:
            raise NotFoundError("approval", approval_id)
        return approval

    def _require_action(self, action_id: str) -> Action:
        action = self.actions.get_by_public_id(action_id)
        if action is None:
            raise NotFoundError("action", action_id)
        return action
