"""Integration test — full LangGraph diagnosis flow with FakeLLM."""

from __future__ import annotations

from datetime import UTC, datetime

from packages.agent.graph import build_graph
from packages.agent.llm import FakeLLMAdapter
from packages.agent.schemas import AgentDeps
from packages.agent.state import IncidentState
from packages.common.settings import Settings
from packages.db.repositories.actions import ActionRepository
from packages.memory.context_builder import ContextBuilder
from packages.memory.memory_store import MemoryStore
from packages.tools.cache import RequestLocalToolCache


def _fake_tool(tool_name: str):
    class FT:
        name: str = tool_name

        def __init__(self, **kw):
            pass

        def run(self, query):
            from packages.tools.base import ToolResult

            return ToolResult(
                status="succeeded", data={}, summary=f"{tool_name} ok", evidence=[], duration_ms=1
            )

    return FT()


def test_graph_runs_end_to_end(db_session) -> None:
    settings = Settings(
        database_url="sqlite://",
        redis_url="memory://",
        celery_broker_url="memory://",
        celery_result_backend="memory://",
    )
    deps = AgentDeps(
        db=db_session,
        settings=settings,
        tool_cache=RequestLocalToolCache(),
        metrics_tool=_fake_tool("metrics"),
        logs_tool=_fake_tool("logs"),
        trace_tool=_fake_tool("traces"),
        git_change_tool=_fake_tool("git_changes"),
        runbook_search_tool=_fake_tool("runbook_search"),
        memory_store=MemoryStore(db_session),
        context_builder=ContextBuilder(),
        llm=FakeLLMAdapter(),
        node_tracer=lambda **kw: None,
        tool_call_recorder=lambda **kw: None,
    )

    graph = build_graph(deps)
    initial: IncidentState = {
        "incident_id": "inc_test",
        "agent_run_id": "run_test",
        "alert_payload": {
            "service": "checkout",
            "severity": "P1",
            "alert_name": "DatabaseConnectionExhaustion",
            "starts_at": datetime(2026, 6, 1, tzinfo=UTC),
        },
        "metrics_evidence": [],
        "logs_evidence": [],
        "traces_evidence": [],
        "deployment_evidence": [],
        "runbook_context": [],
        "memory_context": [],
        "hypotheses": [],
        "root_cause": {},
        "recommended_actions": [],
        "approval_status": {},
        "execution_results": [],
        "incident_report": {},
        "token_budget": {},
        "compression_events": [],
        "errors": [],
        "phase": "initial",
    }

    result = graph.invoke(initial)
    assert result.get("service_name") == "checkout"
    assert len(result.get("hypotheses", [])) >= 1
    assert result.get("root_cause", {}).get("summary")
    assert result.get("phase") == "report_generated"
    assert len(result.get("incident_report", {})) > 0
    actions = list(ActionRepository(db_session).list_for_run("run_test"))
    assert [action.type for action in actions] == [
        "adjust_connection_pool",
        "create_ticket",
    ]
    assert {action.status for action in actions} == {"succeeded"}


def test_graph_handles_all_alert_types(db_session) -> None:
    settings = Settings(
        database_url="sqlite://",
        redis_url="memory://",
        celery_broker_url="memory://",
        celery_result_backend="memory://",
    )
    deps = AgentDeps(
        db=db_session,
        settings=settings,
        tool_cache=RequestLocalToolCache(),
        metrics_tool=_fake_tool("metrics"),
        logs_tool=_fake_tool("logs"),
        trace_tool=_fake_tool("traces"),
        git_change_tool=_fake_tool("git_changes"),
        runbook_search_tool=_fake_tool("runbook_search"),
        memory_store=MemoryStore(db_session),
        context_builder=ContextBuilder(),
        llm=FakeLLMAdapter(),
        node_tracer=lambda **kw: None,
        tool_call_recorder=lambda **kw: None,
    )
    graph = build_graph(deps)

    for alert_name in (
        "DatabaseConnectionExhaustion",
        "High5xxAfterDeploy",
        "RedisCacheAvalanche",
        "PodRestartLoop",
    ):
        initial: IncidentState = {
            "incident_id": f"inc_{alert_name[:4]}",
            "agent_run_id": f"run_{alert_name[:4]}",
            "alert_payload": {
                "service": "checkout",
                "severity": "P2",
                "alert_name": alert_name,
                "starts_at": datetime(2026, 6, 1, tzinfo=UTC),
            },
            "metrics_evidence": [],
            "logs_evidence": [],
            "traces_evidence": [],
            "deployment_evidence": [],
            "runbook_context": [],
            "memory_context": [],
            "hypotheses": [],
            "root_cause": {},
            "recommended_actions": [],
            "approval_status": {},
            "execution_results": [],
            "incident_report": {},
            "token_budget": {},
            "compression_events": [],
            "errors": [],
            "phase": "initial",
            "_needs_approval": False,
            "_all_l4": False,
            "approval_decision": "",
        }
        result = graph.invoke(initial)
        phase = result.get("phase", "")
        # Without checkpointer, L2/L3 alerts interrupt at human_approval
        # and invoke() returns the pre-interrupt state.
        assert phase in ("report_generated", "guardrail_checked"), (
            f"Unexpected phase for {alert_name}: {phase}"
        )
        assert len(result.get("hypotheses", [])) >= 1
        assert result.get("root_cause", {}).get("summary")


def test_graph_runs_multi_perspective_end_to_end(db_session) -> None:
    """Full graph run with multi-perspective diagnosis enabled."""
    from datetime import UTC
    from datetime import datetime as dt

    settings = Settings(
        database_url="sqlite://",
        redis_url="memory://",
        celery_broker_url="memory://",
        celery_result_backend="memory://",
        llm_multi_perspective_enabled=True,
    )
    deps = AgentDeps(
        db=db_session,
        settings=settings,
        tool_cache=RequestLocalToolCache(),
        metrics_tool=_fake_tool("metrics"),
        logs_tool=_fake_tool("logs"),
        trace_tool=_fake_tool("traces"),
        git_change_tool=_fake_tool("git_changes"),
        runbook_search_tool=_fake_tool("runbook_search"),
        memory_store=MemoryStore(db_session),
        context_builder=ContextBuilder(),
        llm=FakeLLMAdapter(),
        node_tracer=lambda **kw: None,
        tool_call_recorder=lambda **kw: None,
    )

    graph = build_graph(deps)
    initial: IncidentState = {
        "incident_id": "inc_mp_test",
        "agent_run_id": "run_mp_test",
        "alert_payload": {
            "service": "checkout",
            "severity": "P1",
            "alert_name": "DatabaseConnectionExhaustion",
            "starts_at": dt(2026, 6, 1, tzinfo=UTC),
        },
        "metrics_evidence": [],
        "logs_evidence": [],
        "traces_evidence": [],
        "deployment_evidence": [],
        "runbook_context": [],
        "memory_context": [],
        "hypotheses": [],
        "root_cause": {},
        "recommended_actions": [],
        "approval_status": {},
        "execution_results": [],
        "incident_report": {},
        "token_budget": {},
        "compression_events": [],
        "errors": [],
        "phase": "initial",
    }

    result = graph.invoke(initial)
    assert result.get("service_name") == "checkout"
    assert len(result.get("hypotheses", [])) >= 1
    assert result.get("root_cause", {}).get("summary")
    llm_calls = result.get("llm_calls", [])
    diagnose_nodes = [c["node"] for c in llm_calls if "diagnose" in c.get("node", "")]
    assert len(diagnose_nodes) >= 5, (
        f"Expected >=5 diagnose calls with multi_perspective, got {diagnose_nodes}"
    )
