from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from apps.api.schemas.common import AgentRunStatus


class AgentRunNode(BaseModel):
    name: str
    status: str
    started_at: datetime | None
    finished_at: datetime | None
    duration_ms: int | None
    input_summary: str | None
    output_summary: str | None
    tool_calls: list[str] = Field(default_factory=list)


class ToolCallSummary(BaseModel):
    tool_call_id: str
    node_name: str
    tool_name: str
    status: str
    input_summary: str
    output_summary: str | None
    duration_ms: int | None
    cache_key: str | None
    cache_hit: bool
    error_message: str | None
    created_at: datetime


class AgentRunSummary(BaseModel):
    agent_run_id: str
    incident_id: str
    status: AgentRunStatus
    celery_task_id: str | None
    created_at: datetime
    updated_at: datetime


class AgentRunDetailResponse(BaseModel):
    agent_run_id: str
    incident_id: str
    status: AgentRunStatus
    celery_task_id: str | None
    error_code: str | None
    error_message: str | None
    state: dict[str, Any]
    checkpoint_thread_id: str | None
    checkpoint_ns: str
    latest_checkpoint_id: str | None
    nodes: list[AgentRunNode] = Field(default_factory=list)
    tool_calls: list[ToolCallSummary] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime
