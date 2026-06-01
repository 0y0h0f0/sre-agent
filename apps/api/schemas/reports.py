from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class IncidentReportResponse(BaseModel):
    report_id: str
    incident_id: str
    agent_run_id: str
    version: int
    root_cause: str
    impact: str
    timeline: list[dict[str, Any]] = Field(default_factory=list)
    actions: list[dict[str, Any]] = Field(default_factory=list)
    follow_ups: list[dict[str, Any] | str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    body_markdown: str
    created_at: datetime
