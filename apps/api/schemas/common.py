from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class Severity(StrEnum):
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"
    P4 = "P4"


class IncidentStatus(StrEnum):
    OPEN = "open"
    DIAGNOSING = "diagnosing"
    WAITING_APPROVAL = "waiting_approval"
    MITIGATED = "mitigated"
    RESOLVED = "resolved"
    FAILED = "failed"


class AgentRunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RiskLevel(StrEnum):
    L0 = "L0"
    L1 = "L1"
    L2 = "L2"
    L3 = "L3"
    L4 = "L4"


class ActionStatus(StrEnum):
    PROPOSED = "proposed"
    BLOCKED = "blocked"
    WAITING_APPROVAL = "waiting_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXECUTING = "executing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class ApprovalStatus(StrEnum):
    WAITING = "waiting"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


class ErrorBody(BaseModel):
    code: str
    message: str
    request_id: str
    details: dict[str, Any] = Field(default_factory=dict)


class ErrorResponse(BaseModel):
    error: ErrorBody


class RootCause(BaseModel):
    summary: str
    confidence: float | None = None
    evidence_ids: list[str] = Field(default_factory=list)


class EvidenceItem(BaseModel):
    evidence_id: str
    type: str
    source: str
    source_id: str | None = None
    source_path: str | None = None
    title: str
    excerpt: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    confidence: float | None = None
    timestamp: datetime | None = None


class ActionSummary(BaseModel):
    action_id: str
    type: str
    risk_level: RiskLevel
    status: ActionStatus
    reason: str
    rollback_plan: str | None = None


class FromAttributesModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class PaginatedResponse(BaseModel):
    """Generic paginated list response."""

    items: list[Any] = Field(default_factory=list)
    total: int = 0
    page: int = 1
    page_size: int = 20
