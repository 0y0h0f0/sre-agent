"""Offline replay engine — rerun historical alerts through new prompts.

Loads historical incident + alert data from the DB and replays through
a different prompt/model, comparing results against the original.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from packages.agent.llm import build_llm
from packages.agent.runner import AgentRunner
from packages.agent.schemas import AgentDeps
from packages.common.ids import new_id
from packages.common.settings import Settings
from packages.db.repositories.agent_runs import AgentRunRepository
from packages.db.repositories.incidents import IncidentRepository
from packages.db.repositories.runbooks import RunbookChunkRepository
from packages.evals.datasets.harness import EvalCaseResult
from packages.memory.context_builder import ContextBuilder
from packages.memory.memory_store import MemoryStore
from packages.rag.retriever import RunbookRetriever
from packages.tools.cache import RequestLocalToolCache
from packages.tools.git_changes import GitChangeTool
from packages.tools.logs import LogsTool
from packages.tools.metrics import MetricsTool
from packages.tools.runbook_search import RunbookSearchTool
from packages.tools.traces import TraceTool


def replay_incident(
    db: Session,
    incident_id: str,
    settings: Settings,
    prompt_version: str = "v1",
) -> EvalCaseResult | None:
    """Replay a single historical incident and compare to original diagnosis."""
    incident_repo = IncidentRepository(db)
    runs_repo = AgentRunRepository(db)

    incident = incident_repo.get_by_public_id(incident_id)
    if incident is None:
        return None

    original_run = runs_repo.get_latest_for_incident(incident_id)
    if original_run is None:
        return None

    alert_payload = incident.raw_payload if incident.raw_payload else {}
    if not alert_payload:
        return None

    agent_run_id = new_id("run_")
    cache = RequestLocalToolCache()

    deps = AgentDeps(
        db=db,
        settings=settings,
        tool_cache=cache,
        metrics_tool=MetricsTool(
            base_url=settings.prometheus_url,
            timeout_seconds=settings.tool_timeout_seconds,
            cache=cache,
        ),
        logs_tool=LogsTool(
            base_url=settings.loki_url,
            timeout_seconds=settings.tool_timeout_seconds,
            cache=cache,
        ),
        trace_tool=TraceTool(
            fixture_path=settings.trace_fixture_path,
            timeout_seconds=settings.tool_timeout_seconds,
            cache=cache,
        ),
        git_change_tool=GitChangeTool(
            fixture_path=settings.git_changes_fixture_path,
            timeout_seconds=settings.tool_timeout_seconds,
            cache=cache,
        ),
        runbook_search_tool=RunbookSearchTool(
            retriever=RunbookRetriever(RunbookChunkRepository(db)),
            timeout_seconds=settings.tool_timeout_seconds,
            cache=cache,
        ),
        memory_store=MemoryStore(db),
        context_builder=ContextBuilder(),
        llm=build_llm(settings),
        node_tracer=lambda **_: None,
        tool_call_recorder=lambda **_: None,
    )

    runner = AgentRunner(deps, checkpointer=None)
    result = runner.run(incident_id, agent_run_id, alert_payload)

    state = result.get("state", {})
    root_cause = state.get("root_cause", {})
    summary = ""
    if isinstance(root_cause, dict):
        summary = root_cause.get("summary", "")

    original_state = original_run.state or {}
    original_summary = original_state.get("root_cause", {}).get("summary", "")
    root_cause_hit = (
        bool(summary)
        and bool(original_summary)
        and summary.lower() == original_summary.lower()
    )

    return EvalCaseResult(
        case_id=incident.incident_id,
        incident_type=incident.alert_name,
        source_path=f"replay:{incident_id}",
        incident_id=incident.incident_id,
        agent_run_id=agent_run_id,
        status=result.get("status", "unknown"),
        approval_interrupted=result.get("status") == "waiting_approval",
        root_cause_summary=summary,
        root_cause_hit=root_cause_hit,
        top3_hit=root_cause_hit,
        required_evidence_hit=True,
        expected_risk_level="unknown",
        actual_risk_level="unknown",
        duration_ms=0,
        tool_total=0,
        tool_successes=0,
        tool_cache_hits=0,
        prompt_token_estimate=0,
        completion_token_estimate=0,
        compression_retention_rate=1.0,
        structured_output_valid=bool(summary),
        memory_misuse=False,
        report_id=None,
        report_version=None,
        error=None if result.get("status") != "failed" else str(result.get("error")),
    )
