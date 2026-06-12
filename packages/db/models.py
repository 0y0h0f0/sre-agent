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
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR

try:
    from pgvector.sqlalchemy import Vector
except Exception:  # pragma: no cover - optional until local deps are installed
    Vector = None
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from packages.common.time import utc_now
from packages.db.base import Base

JSONType = JSON().with_variant(JSONB, "postgresql")
VectorEmbeddingType = JSON() if Vector is None else JSON().with_variant(Vector(512), "postgresql")
TSVectorType = Text().with_variant(TSVECTOR(), "postgresql")


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
    raw_payload: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)
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
    email_token: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True)
    email_token_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


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


class EmailLog(Base):
    __tablename__ = "email_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email_log_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    notification_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    recipients: Mapped[list[str]] = mapped_column(JSONType, nullable=False, default=list)
    recipient_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    related_incident_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    related_agent_run_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    related_approval_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    related_report_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    provider_message_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
        nullable=False,
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RunbookChunk(Base):
    __tablename__ = "runbook_chunks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chunk_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    document_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    source_path: Mapped[str] = mapped_column(String(512), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    embedding: Mapped[list[float]] = mapped_column(
        VectorEmbeddingType, nullable=False, default=list
    )
    embedding_model: Mapped[str] = mapped_column(String(128), nullable=False, default="fake-512")
    tsv_content: Mapped[str | None] = mapped_column("tsv_content", TSVectorType, nullable=True)
    language: Mapped[str] = mapped_column(String(8), nullable=False, default="en")
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


class RunbookDraft(Base):
    __tablename__ = "runbook_drafts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    draft_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    fingerprint: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    incident_ids: Mapped[list[str]] = mapped_column(JSONType, nullable=False, default=list)
    service: Mapped[str] = mapped_column(String(128), nullable=False)
    incident_type: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    front_matter: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="draft", index=True)
    draft_type: Mapped[str] = mapped_column(
        String(32), nullable=False, default="incident_cluster", index=True
    )
    source: Mapped[str] = mapped_column(
        String(64), nullable=False, default="llm"
    )
    discovery_run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    parent_draft_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reviewer: Mapped[str | None] = mapped_column(String(128), nullable=True)
    review_comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_chunk_ids: Mapped[list[str] | None] = mapped_column(JSONType, nullable=True)
    llm_model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
        nullable=False,
    )


class RunbookVersion(Base):
    __tablename__ = "runbook_versions"
    __table_args__ = (
        UniqueConstraint(
            "document_id", "version_number",
            name="uq_runbook_version_document_number",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    version_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    document_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    source_path: Mapped[str] = mapped_column(String(512), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    change_reason: Mapped[str] = mapped_column(String(64), nullable=False)
    related_incident_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    related_draft_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    diff_from_previous: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str] = mapped_column(String(128), nullable=False, default="agent")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
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
    embedding: Mapped[list[float] | None] = mapped_column(VectorEmbeddingType, nullable=True)
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
    suite: Mapped[str] = mapped_column(String(32), nullable=False, default="custom")
    model_name: Mapped[str] = mapped_column(
        String(128), nullable=False, default="fake-diagnosis-model"
    )
    prompt_version: Mapped[str] = mapped_column(String(64), nullable=False, default="v1")
    metrics: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    git_commit: Mapped[str] = mapped_column(String(64), nullable=False, default="unknown")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class EvalCase(Base):
    __tablename__ = "eval_cases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    eval_case_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    eval_run_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("eval_runs.eval_run_id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    incident_type: Mapped[str] = mapped_column(String(128), nullable=False)
    fixture_path: Mapped[str] = mapped_column(String(512), nullable=False)
    expected_root_cause: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    actual_root_cause: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONType, nullable=False, default=dict
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class FalsePositivePattern(Base):
    """Tracks NFA (Not Actionable Alert) markings per fingerprint.

    After `nfa_auto_suppress_threshold` marks the alert severity is
    auto-degraded to P4. Stale patterns reset after `nfa_reset_days`.
    """

    __tablename__ = "false_positive_patterns"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    pattern_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    fingerprint: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    service: Mapped[str] = mapped_column(String(128), nullable=False)
    alert_name: Mapped[str] = mapped_column(String(255), nullable=False)
    nfa_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="active", index=True
    )
    first_nfa_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    last_nfa_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    suppressed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    suppressed_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    restored_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    restored_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class IncidentCorrelation(Base):
    """Cross-incident association records.

    Links incidents by same fingerprint, similar embedding, or manual association.
    """

    __tablename__ = "incident_correlations"
    __table_args__ = (
        UniqueConstraint(
            "incident_id_a", "incident_id_b",
            name="uq_correlation_pair",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    correlation_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    incident_id_a: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    incident_id_b: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    correlation_type: Mapped[str] = mapped_column(
        String(32), nullable=False
    )  # same_fingerprint | similar_embedding | similar_service | manual
    similarity_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class FeedbackItem(Base):
    """User corrections to diagnosis — root cause rewrites, action additions/removals.

    Deltas are recorded for audit and future eval dataset construction.
    Model fine-tuning requires separate governance; this table only stores auditable
    feedback data.
    """

    __tablename__ = "feedback_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    feedback_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    incident_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    agent_run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    feedback_type: Mapped[str] = mapped_column(
        String(32), nullable=False, index=True
    )  # root_cause_correction | action_addition | action_removal | nfa_mark
    original_value: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)
    corrected_value: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)
    delta: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)
    submitted_by: Mapped[str] = mapped_column(String(128), nullable=False, default="sre")
    submitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class IncidentComment(Base):
    """Multi-person comments on an incident with @mention support.

    Supports threaded replies via ``parent_comment_id``.
    """

    __tablename__ = "incident_comments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    comment_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    incident_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("incidents.incident_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    author: Mapped[str] = mapped_column(String(128), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    parent_comment_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    mentioned_users: Mapped[list[str]] = mapped_column(JSONType, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class EvidenceAnnotation(Base):
    """Annotations on evidence items by SRE team members."""

    __tablename__ = "evidence_annotations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    annotation_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    evidence_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("evidence_items.evidence_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    incident_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("incidents.incident_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    author: Mapped[str] = mapped_column(String(128), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class AuditLog(Base):
    """Write-ahead audit log recording who did what and when.

    Tracks approval decisions, root cause corrections, NFA marks,
    action feedback, comment/annotation creation, discovery/config operations.
    """

    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_logs_source_created", "source", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    audit_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    incident_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("incidents.incident_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    actor: Mapped[str] = mapped_column(String(128), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    resource_type: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(64), nullable=False)
    # Operation origin: api | worker | beat | system
    source: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    # Correlates operations across services (matches X-Request-Id header).
    request_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    details: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False, index=True
    )


class ApprovalGroup(Base):
    """Approval groups keyed by service pattern for team-based routing.

    Members are a JSON list of approver names. When an approval is
    created for a service matching ``service_pattern``, the groupʼs
    members receive the notification in addition to the global list.
    """

    __tablename__ = "approval_groups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    group_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    service_pattern: Mapped[str] = mapped_column(String(255), nullable=False)
    members: Mapped[list[str]] = mapped_column(JSONType, nullable=False, default=list)
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
        nullable=False,
    )


class ApiKey(Base):
    """API key for service-to-service and management authentication.

    Raw key is returned once on creation and stored as a SHA-256 hash.
    Roles and scopes control access to config/discovery write APIs.
    """

    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    description: Mapped[str] = mapped_column(String(255), nullable=False)
    key_hash: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    created_by: Mapped[str] = mapped_column(String(128), nullable=False, default="admin")
    # Role/scope access control (M0 PR 0.7).
    # roles: list of role names (e.g., ["api_key:admin"]).
    roles: Mapped[list[str]] = mapped_column(JSONType, nullable=False, default=list)
    # scopes: granular permission tokens
    # (discovery:read, discovery:write, config:read, config:write,
    #  runbook:review, api_key:admin).
    scopes: Mapped[list[str]] = mapped_column(JSONType, nullable=False, default=list)
    # Marks keys created during bootstrap seeding.
    is_bootstrap: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class RunbookFeedbackSummary(Base):
    """Aggregated runbook feedback for a (service, fault_type) group.

    Created deterministically when >= runbook_amendment_min_incidents
    incidents of the same (service, fault_type) have been resolved.
    No LLM / web_search involved — purely statistical aggregation.
    """

    __tablename__ = "runbook_feedback_summaries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    summary_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    service: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    fault_type: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    incident_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    incident_ids: Mapped[list[str]] = mapped_column(JSONType, nullable=False, default=list)
    # Action statistics
    total_actions: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    successful_actions: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_actions: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    skipped_actions: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rejected_actions: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Top action types for this fault_type
    top_action_types: Mapped[dict[str, int]] = mapped_column(JSONType, nullable=False, default=dict)
    # Gap detection results
    missing_fault_types: Mapped[list[str]] = mapped_column(
        JSONType, nullable=False, default=list
    )
    missing_diagnostic_steps: Mapped[list[str]] = mapped_column(
        JSONType, nullable=False, default=list
    )
    recurring_evidence_patterns: Mapped[list[str]] = mapped_column(
        JSONType, nullable=False, default=list
    )
    # Metadata
    cooldown_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    generated_by: Mapped[str] = mapped_column(
        String(128), nullable=False, default="runbook_feedback"
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


class AmendmentDraft(Base):
    """Proposed runbook amendment derived from deterministic feedback analysis.

    Points to a RunbookFeedbackSummary for provenance. Amendments enter the
    review queue as pending_review — they are never ingested automatically.
    No LLM / web_search involved in Phase 0-8.
    """

    __tablename__ = "amendment_drafts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    amendment_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    summary_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("runbook_feedback_summaries.summary_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    service: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    fault_type: Mapped[str] = mapped_column(String(128), nullable=False)
    # The target runbook draft this amendment proposes to change
    target_draft_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Amendment content — proposed additions/changes to the runbook
    amendment_type: Mapped[str] = mapped_column(
        String(32), nullable=False, default="addition"
    )  # addition | correction | removal
    section_path: Mapped[str | None] = mapped_column(String(255), nullable=True)
    original_content: Mapped[str | None] = mapped_column(Text, nullable=True)
    proposed_content: Mapped[str] = mapped_column(Text, nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    # Evidence from feedback analysis
    evidence_incident_ids: Mapped[list[str]] = mapped_column(
        JSONType, nullable=False, default=list
    )
    evidence_action_stats: Mapped[dict[str, Any]] = mapped_column(
        JSONType, nullable=False, default=dict
    )
    # Review state — amendments always enter review queue
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="pending_review", index=True
    )
    reviewer: Mapped[str | None] = mapped_column(String(128), nullable=True)
    review_comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
        nullable=False,
    )


class DiscoveryRun(Base):
    """Record of a single discovery scan execution.

    May be triggered by Celery Beat schedule, manual operator rerun,
    or application startup (local only).
    """

    __tablename__ = "discovery_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    discovery_run_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    source: Mapped[str] = mapped_column(
        String(32), nullable=False
    )  # scheduled | manual_rerun | startup
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="running", index=True
    )  # running | succeeded | degraded | failed
    trigger_type: Mapped[str] = mapped_column(
        String(32), nullable=False, default="automatic"
    )  # automatic | manual
    triggered_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Summary of discovery findings (service count, backend count, warnings).
    summary: Mapped[dict[str, Any]] = mapped_column(
        JSONType, nullable=False, default=dict
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class DiscoveryProposal(Base):
    """A config change proposal produced by a discovery run.

    Each proposal contains a config_diff (what changed vs current effective config)
    and an AutomationDecision for each changed item.
    """

    __tablename__ = "discovery_proposals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    proposal_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    discovery_run_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("discovery_runs.discovery_run_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="pending_review", index=True
    )  # pending_review | auto_applied | rejected | superseded
    # The config diff as a JSONB document (add/update/delete actions).
    config_diff: Mapped[dict[str, Any]] = mapped_column(
        JSONType, nullable=False, default=dict
    )
    # Overall confidence across all diff items (0.0–1.0).
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    applied_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    rejected_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewed_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class EffectiveConfigVersion(Base):
    """A published version of the effective runtime configuration.

    Workers read the latest published version (status='published') when
    constructing AgentDeps. Stale configs continue to be used but produce
    a warning metric.
    """

    __tablename__ = "effective_config_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    version_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    proposal_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("discovery_proposals.proposal_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="published", index=True
    )  # published | rolled_back | revoked | superseded
    # The full effective config snapshot at publish time.
    config_snapshot: Mapped[dict[str, Any]] = mapped_column(
        JSONType, nullable=False, default=dict
    )
    published_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    published_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    rolled_back_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Number of days after published_at before config is considered stale.
    # Default 30 days; stale config still used by workers but emits warning.
    stale_after_days: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    stale_warning_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class DiscoveryOverride(Base):
    """Operator-created override for a specific backend configuration.

    Active override = revoked_at IS NULL AND expires_at > now().
    Expired or revoked overrides do not participate in EffectiveConfig merge
    but are retained for audit.
    """

    __tablename__ = "discovery_overrides"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    override_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    # The backend type being overridden: prometheus | loki | jaeger | alertmanager
    backend_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    # Override values as JSONB (url, auth_type, extra_params, etc.).
    override_json: Mapped[dict[str, Any]] = mapped_column(
        JSONType, nullable=False, default=dict
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    revoke_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by_key_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_by_scopes: Mapped[list[str]] = mapped_column(
        JSONType, nullable=False, default=list
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


class AlertPollCursor(Base):
    """Cursor state for Alertmanager poll dedup and resolved inference.

    Records per-filter-hash fingerprint tracking. The fingerprint ->
    incident_id mapping is globally unique (cross filter-hash + webhook dedup).

    already_seen_active() MUST update last_seen_at and reset missing_rounds
    even when returning True (intentional side effect).
    """

    __tablename__ = "alert_poll_cursors"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    filter_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    fingerprint: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    incident_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    missing_rounds: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )

    __table_args__ = (
        UniqueConstraint("filter_hash", "fingerprint", name="uq_poll_cursor_filter_fp"),
    )


Index("ix_incidents_service_created_at", Incident.service, Incident.created_at.desc())
Index("ix_email_log_created_at", EmailLog.created_at.desc())
Index("ix_incidents_status_severity", Incident.status, Incident.severity)
Index(
    "uq_incidents_open_fingerprint",
    Incident.fingerprint,
    unique=True,
    sqlite_where=text("status NOT IN ('resolved', 'failed', 'mitigated')"),
    postgresql_where=text("status NOT IN ('resolved', 'failed', 'mitigated')"),
)
