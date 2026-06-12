"""Agent Pydantic schemas — diagnosis output, hypotheses, actions, deps."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from packages.common.settings import Settings
from packages.memory.context_builder import ContextBuilder
from packages.memory.memory_store import MemoryStore
from packages.tools.base import BaseTool
from packages.tools.cache import RequestLocalToolCache
from packages.tools.executor_backends import ExecutorBackend


class Hypothesis(BaseModel):
    id: str = ""
    statement: str = ""
    supporting_evidence_ids: list[str] = Field(default_factory=list)
    confidence: float = 0.0
    rank_explanation: str = ""


class DiagnosisOutput(BaseModel):
    hypotheses: list[Hypothesis] = Field(default_factory=list)
    root_cause: dict[str, Any] = Field(default_factory=dict)
    evidence_ids: list[str] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)


class RankedHypothesis(Hypothesis):
    rank: int = 0
    evidence_count: int = 0
    source_diversity: int = 0
    deployment_correlation: float = 0.0
    runbook_match_score: float = 0.0
    memory_similarity_score: float = 0.0


class PlannedAction(BaseModel):
    type: str = ""
    target: str = ""
    params: dict[str, Any] = Field(default_factory=dict)
    reason: str = ""
    risk_hint: str = ""
    rollback_plan: str = ""


class GuardrailDecision(BaseModel):
    action_id: str = ""
    risk_level: str = "L0"
    allowed: bool = True
    requires_approval: bool = False
    reason: str = ""


class AgentDeps:
    """Dependency injection container for agent nodes."""

    def __init__(
        self,
        *,
        db: Session,
        settings: Settings,
        tool_cache: RequestLocalToolCache,
        metrics_tool: BaseTool,
        logs_tool: BaseTool,
        trace_tool: BaseTool,
        git_change_tool: BaseTool,
        runbook_search_tool: BaseTool,
        memory_store: MemoryStore,
        context_builder: ContextBuilder,
        llm: Any,
        node_tracer: Callable[..., None],
        tool_call_recorder: Callable[..., None],
        k8s_tool: BaseTool | None = None,
        db_diagnostics_tool: BaseTool | None = None,
        executor_backend: ExecutorBackend | None = None,
        effective_config: Any | None = None,
        config_version_id: str | None = None,
    ) -> None:
        self.db = db
        self.settings = settings
        self.tool_cache = tool_cache
        self.metrics_tool = metrics_tool
        self.logs_tool = logs_tool
        self.trace_tool = trace_tool
        self.git_change_tool = git_change_tool
        self.runbook_search_tool = runbook_search_tool
        # Phase 2.2/2.3 read-only diagnosis tools (optional; default None keeps
        # existing call sites and the deterministic test harness unchanged).
        self.k8s_tool = k8s_tool
        self.db_diagnostics_tool = db_diagnostics_tool
        self.executor_backend = executor_backend
        self.memory_store = memory_store
        self.context_builder = context_builder
        self.llm = llm
        self.node_tracer = node_tracer
        self.tool_call_recorder = tool_call_recorder
        # M5 PR 5.5: effective config and version tracking.
        self.effective_config = effective_config
        self.config_version_id = config_version_id
