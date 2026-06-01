from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from apps.api.schemas.common import (
    ActionSummary,
    EvidenceItem,
    IncidentStatus,
    RootCause,
    Severity,
)


class IncidentListItem(BaseModel):
    incident_id: str
    service: str
    severity: Severity
    status: IncidentStatus
    alert_name: str
    root_cause_summary: str | None
    created_at: datetime
    updated_at: datetime


class IncidentDetailResponse(BaseModel):
    incident_id: str
    service: str
    severity: Severity
    status: IncidentStatus
    alert: dict[str, Any]
    root_cause: RootCause | None
    evidence: list[EvidenceItem] = Field(default_factory=list)
    recommended_actions: list[ActionSummary] = Field(default_factory=list)


class DiagnoseRequest(BaseModel):
    force: bool = False
    reason: str | None = Field(default=None, max_length=500)


class DiagnoseResponse(BaseModel):
    incident_id: str
    agent_run_id: str
    celery_task_id: str
    status: str
