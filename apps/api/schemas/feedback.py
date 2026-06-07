"""Pydantic schemas for Phase 5 feedback, NFA, and cross-incident correlation."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class NfaMarkRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=500)


class NfaMarkResponse(BaseModel):
    pattern_id: str
    fingerprint: str
    nfa_count: int
    status: str  # active | suppressed
    message: str


class RootCauseCorrectionRequest(BaseModel):
    corrected_summary: str = Field(min_length=1, max_length=2000)
    reason: str | None = Field(default=None, max_length=500)


class ActionCorrectionRequest(BaseModel):
    action_type: str  # "add" | "remove"
    action: dict[str, Any] | None = None  # required for "add"
    action_id: str | None = None  # required for "remove"
    reason: str | None = Field(default=None, max_length=500)


class CorrelatedIncident(BaseModel):
    incident_id: str
    service: str
    severity: str
    alert_name: str
    root_cause_summary: str | None
    correlation_type: str  # same_fingerprint | similar_service
    similarity_score: float | None
    created_at: datetime


class FeedbackResponse(BaseModel):
    feedback_id: str
    incident_id: str
    feedback_type: str
    original_value: dict[str, Any] | None
    corrected_value: dict[str, Any] | None
    delta: dict[str, Any] | None
    submitted_by: str
    submitted_at: datetime


class FeedbackListResponse(BaseModel):
    items: list[FeedbackResponse] = Field(default_factory=list)
    total: int = 0
