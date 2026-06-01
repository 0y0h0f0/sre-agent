from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from apps.api.schemas.common import ActionStatus, ApprovalStatus, RiskLevel


class ApproveRequest(BaseModel):
    approver: str = Field(..., min_length=1, max_length=128)
    comment: str | None = Field(default=None, max_length=1000)
    risk_ack: bool = False
    confirm_action_type: str | None = Field(default=None, max_length=128)
    confirm_target: str | None = Field(default=None, max_length=255)


class RejectRequest(BaseModel):
    approver: str = Field(..., min_length=1, max_length=128)
    comment: str | None = Field(default=None, max_length=1000)


class ApprovalItem(BaseModel):
    approval_id: str
    action_id: str
    incident_id: str
    agent_run_id: str
    service: str
    action_type: str
    risk_level: RiskLevel
    approval_status: ApprovalStatus
    action_status: ActionStatus
    reason: str
    rollback_plan: str | None = None
    requested_at: datetime
    decided_at: datetime | None = None
    approver: str | None = None
    comment: str | None = None


class ApprovalDecisionResponse(BaseModel):
    approval_id: str
    action_id: str
    status: ApprovalStatus
    agent_run_id: str
