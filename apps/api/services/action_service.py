"""Business logic for action detail and execution."""

from __future__ import annotations

from sqlalchemy.orm import Session

from apps.api.schemas.actions import (
    ActionDetailResponse,
    ExecuteRequest,
    ExecuteResponse,
)
from apps.api.schemas.common import ActionStatus, RiskLevel
from packages.common.errors import (
    ApprovalRequiredError,
    NotFoundError,
    ValidationAppError,
)
from packages.common.ids import new_id
from packages.db.models import Action
from packages.db.repositories.actions import ActionRepository
from packages.db.repositories.approvals import ApprovalRepository
from packages.tools.executor_backends import ExecutionContext, FixtureExecutorBackend


class ActionService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.actions = ActionRepository(db)
        self.approvals = ApprovalRepository(db)

    def get_detail(self, action_id: str) -> ActionDetailResponse:
        action = self._require_action(action_id)
        return ActionDetailResponse(
            action_id=action.action_id,
            incident_id=action.incident_id,
            agent_run_id=action.agent_run_id,
            type=action.type,
            risk_level=RiskLevel(action.risk_level),
            status=ActionStatus(action.status),
            executor=action.executor,
            target=action.target,
            params=action.params,
            reason=action.reason,
            rollback_plan=action.rollback_plan,
            execution_result=action.execution_result,
            created_at=action.created_at,
            updated_at=action.updated_at,
        )

    def execute(self, action_id: str, request: ExecuteRequest) -> ExecuteResponse:
        action = self._require_action(action_id)
        risk_level = action.risk_level

        # L4 actions are always rejected
        if risk_level == "L4":
            action.status = ActionStatus.BLOCKED.value
            action.execution_result = {
                "status": "blocked",
                "message": "L4 destructive actions are permanently blocked",
            }
            self.db.commit()
            raise ApprovalRequiredError(
                "L4 actions cannot be executed",
                details={"action_id": action_id, "risk_level": "L4"},
            )

        # L2/L3 actions require an approved approval
        if risk_level in ("L2", "L3"):
            approved_approval = self.approvals.get_approved_for_action(action_id)
            if approved_approval is None:
                raise ApprovalRequiredError(
                    "Action requires an approved approval before execution",
                    details={"action_id": action_id, "risk_level": risk_level},
                )

            # L3 also requires that secondary confirmation fields were saved
            if risk_level == "L3":
                if not approved_approval.risk_ack:
                    raise ApprovalRequiredError(
                        "L3 action missing risk_ack confirmation",
                        details={"action_id": action_id, "missing": "risk_ack"},
                    )
                if not approved_approval.confirm_action_type:
                    raise ApprovalRequiredError(
                        "L3 action missing confirm_action_type",
                        details={"action_id": action_id, "missing": "confirm_action_type"},
                    )
                if approved_approval.confirm_action_type != action.type:
                    raise ApprovalRequiredError(
                        "L3 action confirm_action_type mismatch",
                        details={
                            "action_id": action_id,
                            "expected": action.type,
                            "provided": approved_approval.confirm_action_type,
                        },
                    )
                if not approved_approval.confirm_target:
                    raise ApprovalRequiredError(
                        "L3 action missing confirm_target",
                        details={"action_id": action_id, "missing": "confirm_target"},
                    )
                expected_target = action.target or ""
                if approved_approval.confirm_target != expected_target:
                    raise ApprovalRequiredError(
                        "L3 action confirm_target mismatch",
                        details={
                            "action_id": action_id,
                            "expected": expected_target,
                            "provided": approved_approval.confirm_target,
                        },
                    )

        # Validate not already executed
        if action.status in (ActionStatus.EXECUTING.value, ActionStatus.SUCCEEDED.value):
            raise ValidationAppError(
                "action already executed or in progress",
                details={"action_id": action_id, "status": action.status},
            )

        # Mark executing
        action.status = ActionStatus.EXECUTING.value
        self.db.flush()

        # Execute via fixture executor (same backend as the graph node).
        # The fixture backend is deterministic by action type alone, so the
        # ExecutionContext fields are passed for audit consistency only.
        backend = FixtureExecutorBackend()
        exec_result = backend.execute(
            {"type": action.type, "target": action.target or "", "params": action.params or {}},
            ExecutionContext(
                service="",
                incident_id=action.incident_id,
                agent_run_id=action.agent_run_id,
            ),
        )

        # Update action status
        action.status = ActionStatus.SUCCEEDED.value
        action.execution_result = exec_result.model_dump()
        self.db.commit()

        execution_id = new_id("exec_")
        return ExecuteResponse(
            action_id=action_id,
            status=ActionStatus.SUCCEEDED,
            execution_id=execution_id,
        )

    def _require_action(self, action_id: str) -> Action:
        action = self.actions.get_by_public_id(action_id)
        if action is None:
            raise NotFoundError("action", action_id)
        return action
