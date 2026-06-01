"""Database models for the incident response agent."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB

try:
    from pgvector.sqlalchemy import Vector
except Exception:  # pragma: no cover - optional until local deps are installed
    Vector = None
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from packages.common.time import utc_now
from packages.db.base import Base

JSONType = JSON().with_variant(JSONB, "postgresql")
Vector384Type = JSON() if Vector is None else JSON().with_variant(Vector(384), "postgresql")


class Incident(Base):
    __tablename__ = "incidents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    incident_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    fingerprint: Mapped[str] = mapped_column(String(255), nullable=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    service: Mapped[str] = mapped_column(String(128), nullable=False)
    severity: Mapped[str] = mapped_column(String(8), nullable=False)
    alert_name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="open", index=True)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    labels: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)
    annotations: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)
    root_cause_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
        nullable=False,
    )

    agent_runs: Mapped[list[AgentRun]] = relationship(
        back_populates="incident",
        cascade="all, delete-orphan",
        primaryjoin="Incident.incident_id == AgentRun.incident_id",
    )
    evidence_items: Mapped[list[EvidenceItem]] = relationship(
        back_populates="incident",
        cascade="all, delete-orphan",
        primaryjoin="Incident.incident_id == EvidenceItem.incident_id",
    )
    actions: Mapped[list[Action]] = relationship(
        back_populates="incident",
        cascade="all, delete-orphan",
        primaryjoin="Incident.incident_id == Action.incident_id",
    )


class AgentRun(Base):
    __tablename__ = "agent_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    agent_run_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    incident_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("incidents.incident_id"),
        nullable=False,
        index=True,
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued", index=True)
    celery_task_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    model_name: Mapped[str] = mapped_column(
        String(128), nullable=False, default="fake-diagnosis-model"
    )
    prompt_version: Mapped[str] = mapped_column(String(64), nullable=False, default="v1")
    state: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)
    checkpoint_thread_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    checkpoint_ns: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    latest_checkpoint_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    total_prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    provider_cache_hit_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    provider_cache_miss_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    app_cache_hit_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    app_cache_miss_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
        nullable=False,
    )

    incident: Mapped[Incident] = relationship(back_populates="agent_runs")
    nodes: Mapped[list[AgentRunNode]] = relationship(
        back_populates="agent_run",
        cascade="all, delete-orphan",
        primaryjoin="AgentRun.agent_run_id == AgentRunNode.agent_run_id",
    )
    tool_calls: Mapped[list[ToolCall]] = relationship(
        back_populates="tool_run",
        cascade="all, delete-orphan",
        primaryjoin="AgentRun.agent_run_id == ToolCall.agent_run_id",
    )


class AgentRunNode(Base):
    __tablename__ = "agent_run_nodes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    node_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    agent_run_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("agent_runs.agent_run_id"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    input_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    output_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )

    agent_run: Mapped[AgentRun] = relationship(back_populates="nodes")


class ToolCall(Base):
    __tablename__ = "tool_calls"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tool_call_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    agent_run_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("agent_runs.agent_run_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    node_name: Mapped[str] = mapped_column(String(128), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(128), nullable=False)
    input_json: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)
    input_summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    output_json: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)
    output_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cache_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    cache_hit: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )

    tool_run: Mapped[AgentRun] = relationship(back_populates="tool_calls")


class EvidenceItem(Base):
    __tablename__ = "evidence_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    evidence_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    incident_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("incidents.incident_id"),
        nullable=False,
        index=True,
    )
    agent_run_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("agent_runs.agent_run_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    source: Mapped[str] = mapped_column(String(128), nullable=False)
    source_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    excerpt: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    timestamp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )

    incident: Mapped[Incident] = relationship(back_populates="evidence_items")


class Action(Base):
    __tablename__ = "actions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    action_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    incident_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("incidents.incident_id"),
        nullable=False,
        index=True,
    )
    agent_run_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("agent_runs.agent_run_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    type: Mapped[str] = mapped_column(String(128), nullable=False)
    risk_level: Mapped[str] = mapped_column(String(8), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    executor: Mapped[str] = mapped_column(String(64), nullable=False, default="mock")
    target: Mapped[str | None] = mapped_column(String(255), nullable=True)
    params: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    rollback_plan: Mapped[str | None] = mapped_column(Text, nullable=True)
    execution_result: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
        nullable=False,
    )

    incident: Mapped[Incident] = relationship(back_populates="actions")


class Approval(Base):
    __tablename__ = "approvals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    approval_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    action_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("actions.action_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    incident_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("incidents.incident_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    agent_run_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("agent_runs.agent_run_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="waiting", index=True)
    approver: Mapped[str | None] = mapped_column(String(128), nullable=True)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    risk_ack: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    confirm_action_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    confirm_target: Mapped[str | None] = mapped_column(String(255), nullable=True)
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resume_token: Mapped[str | None] = mapped_column(String(255), nullable=True)


class IncidentReport(Base):
    __tablename__ = "incident_reports"
    __table_args__ = (
        UniqueConstraint("incident_id", "version", name="uq_report_incident_version"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    report_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    incident_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("incidents.incident_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    agent_run_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("agent_runs.agent_run_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    root_cause: Mapped[str] = mapped_column(Text, nullable=False)
    impact: Mapped[str] = mapped_column(Text, nullable=False)
    timeline: Mapped[list[dict[str, Any]]] = mapped_column(JSONType, nullable=False, default=list)
    actions: Mapped[list[dict[str, Any]]] = mapped_column(JSONType, nullable=False, default=list)
    follow_ups: Mapped[list[dict[str, Any] | str]] = mapped_column(
        JSONType, nullable=False, default=list
    )
    body_markdown: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class RunbookChunk(Base):
    __tablename__ = "runbook_chunks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chunk_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    document_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    source_path: Mapped[str] = mapped_column(String(512), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    embedding: Mapped[list[float]] = mapped_column(Vector384Type, nullable=False, default=list)
    embedding_model: Mapped[str] = mapped_column(String(128), nullable=False, default="fake-384")
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONType, nullable=False, default=dict
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
        nullable=False,
    )


class MemoryItem(Base):
    __tablename__ = "memory_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    memory_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    scope: Mapped[str] = mapped_column(String(32), nullable=False)
    scope_key: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    memory_type: Mapped[str] = mapped_column(String(32), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_json: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)
    embedding: Mapped[list[float] | None] = mapped_column(Vector384Type, nullable=True)
    importance: Mapped[float] = mapped_column(Float, nullable=False, default=0.5)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    source_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
        nullable=False,
    )


class MemoryEvent(Base):
    __tablename__ = "memory_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    agent_run_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("agent_runs.agent_run_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    node_name: Mapped[str] = mapped_column(String(128), nullable=False)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    before_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    after_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    compression_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONType, nullable=False, default=dict
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class EvalRun(Base):
    __tablename__ = "eval_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    eval_run_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class EvalCase(Base):
    __tablename__ = "eval_cases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    eval_case_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    incident_type: Mapped[str] = mapped_column(String(128), nullable=False)
    fixture_path: Mapped[str] = mapped_column(String(512), nullable=False)
    expected_root_cause: Mapped[str] = mapped_column(Text, nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONType, nullable=False, default=dict
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


Index("ix_incidents_service_created_at", Incident.service, Incident.created_at.desc())
Index("ix_incidents_status_severity", Incident.status, Incident.severity)
Index(
    "uq_incidents_open_fingerprint",
    Incident.fingerprint,
    unique=True,
    sqlite_where=text("status NOT IN ('resolved', 'failed', 'mitigated')"),
    postgresql_where=text("status NOT IN ('resolved', 'failed', 'mitigated')"),
)
