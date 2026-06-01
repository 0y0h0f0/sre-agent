from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from apps.api.schemas.common import ActionStatus, RiskLevel


class ExecuteRequest(BaseModel):
    operator: str = Field(..., min_length=1, max_length=128)
    reason: str | None = Field(default=None, max_length=500)


class ActionDetailResponse(BaseModel):
    action_id: str
    incident_id: str
    agent_run_id: str
    type: str
    risk_level: RiskLevel
    status: ActionStatus
    executor: str = "mock"
    target: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    reason: str
    rollback_plan: str | None = None
    execution_result: dict[str, Any] | None = None
    created_at: datetime
    updated_at: datetime


class ExecuteResponse(BaseModel):
    action_id: str
    status: ActionStatus
    execution_id: str = ""
