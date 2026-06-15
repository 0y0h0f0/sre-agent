"""Integration test — full LangGraph diagnosis flow with FakeLLM."""

from __future__ import annotations

from datetime import UTC, datetime

from packages.agent.graph import build_graph
from packages.agent.llm import FakeLLMAdapter
from packages.agent.nodes.execute_action import execute_action
from packages.agent.nodes.human_approval import human_approval
from packages.agent.nodes.plan_actions import plan_actions
from packages.agent.nodes.take_snapshot import take_snapshot
from packages.agent.nodes.verify import verify
from packages.agent.schemas import AgentDeps
from packages.agent.state import IncidentState
from packages.common.settings import Settings
from packages.db.models import AgentRun, Incident
from packages.db.repositories.actions import ActionRepository
from packages.memory.context_builder import ContextBuilder
from packages.memory.memory_store import MemoryStore
from packages.tools.base import ToolResult
from packages.tools.cache import RequestLocalToolCache
from packages.tools.executor_backends import ExecutionResult


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


def _seed_incident_run(db_session, incident_id: str, agent_run_id: str) -> None:
    db_session.add(
        Incident(
            incident_id=incident_id,
            fingerprint=f"fp_{incident_id}",
            source="mock",
            service="checkout",
            severity="P1",
            alert_name="High5xxAfterDeploy",
            status="diagnosing",
            starts_at=datetime(2026, 6, 1, tzinfo=UTC),
            labels={},
            annotations={},
        )
    )
    db_session.add(
        AgentRun(agent_run_id=agent_run_id, incident_id=incident_id, status="running")
    )
    db_session.flush()


class _K8sSnapshotThenFailedRolloutTool:
    name = "k8s"
    timeout_seconds = 1.0

    def run(self, query):
        if query.operation == "get_deployment":
            return ToolResult(
                status="succeeded",
                data={
                    "payload": {
                        "name": query.service,
                        "namespace": query.namespace,
                        "replicas": 2,
                        "ready_replicas": 2,
                        "available_replicas": 2,
                        "image": "checkout:v1",
                        "conditions": [{"type": "Progressing", "status": "True"}],
                    }
                },
                summary="deployment snapshot",
                duration_ms=1,
            )
        if query.operation == "rollout_status":
            return ToolResult(
                status="succeeded",
                data={
                    "payload": {
                        "deployment": query.service,
                        "desired_replicas": 2,
                        "updated_replicas": 1,
                        "ready_replicas": 0,
                        "status": "failed",
                    }
                },
                summary="rollout failed",
                evidence=[
                    {"type": "k8s", "source": "fixture", "summary": "rollout failed"}
                ],
                duration_ms=1,
            )
        raise AssertionError(f"unexpected k8s operation {query.operation}")


class _StaticTool:
    timeout_seconds = 1.0

    def __init__(self, name: str, result: ToolResult) -> None:
        self.name = name
        self.result = result

    def run(self, query):
        return self.result


class _RecordingLiveBackend:
    name = "live"

    def __init__(self) -> None:
        self.execute_calls = []

    def execute(self, action, context):
        self.execute_calls.append((action, context))
        return ExecutionResult(status="succeeded", message="live restart")

    def rollback(self, action, snapshot, context):
        raise AssertionError("rollback is planned after verify, not executed in this smoke")


class _RollbackPlannerLLM:
    def __init__(self) -> None:
        self.prompt = ""

    def generate_json(self, prompt, output_schema, *, thinking=False):
        from packages.agent.schemas import PlannedAction

        self.prompt = prompt
        return [
            PlannedAction(
                type="rollback_release",
                target="checkout",
                params={"revision": "7"},
                reason="rollout verification degraded after restart",
                risk_hint="L3",
                rollback_plan="verify rollout and metrics after rollback",
            )
        ]


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


def test_approval_snapshot_execute_verify_degraded_replans_to_l3_rollback(
    db_session, monkeypatch
) -> None:
    """Smoke the approved live path without any real external service."""
    incident_id = "inc_pr6_smoke"
    agent_run_id = "run_pr6_smoke"
    _seed_incident_run(db_session, incident_id, agent_run_id)
    settings = Settings(
        database_url="sqlite://",
        redis_url="memory://",
        celery_broker_url="memory://",
        celery_result_backend="memory://",
        executor_k8s_namespace="default",
    )
    backend = _RecordingLiveBackend()
    llm = _RollbackPlannerLLM()
    deps = AgentDeps(
        db=db_session,
        settings=settings,
        tool_cache=RequestLocalToolCache(),
        metrics_tool=_StaticTool(
            "metrics",
            ToolResult(
                status="succeeded",
                data={},
                summary="error_rate=0.005",
                evidence=[
                    {"type": "metric", "source": "prometheus", "summary": "error_rate=0.005"}
                ],
                duration_ms=1,
            ),
        ),
        logs_tool=_StaticTool(
            "logs",
            ToolResult(status="succeeded", data={}, summary="clean", duration_ms=1),
        ),
        trace_tool=_fake_tool("traces"),
        git_change_tool=_fake_tool("git_changes"),
        runbook_search_tool=_fake_tool("runbook_search"),
        memory_store=MemoryStore(db_session),
        context_builder=ContextBuilder(),
        llm=llm,
        node_tracer=lambda **kw: None,
        tool_call_recorder=lambda **kw: None,
        k8s_tool=_K8sSnapshotThenFailedRolloutTool(),
        executor_backend=backend,
    )
    monkeypatch.setattr("packages.agent.nodes.verify.time.sleep", lambda _: None)
    state: IncidentState = {
        "incident_id": incident_id,
        "agent_run_id": agent_run_id,
        "service_name": "checkout",
        "alert_name": "High5xxAfterDeploy",
        "recommended_actions": [
            {
                "type": "restart_service",
                "target": "checkout",
                "params": {},
                "reason": "restart stuck deployment",
                "risk_level": "L2",
                "allowed": True,
                "requires_approval": True,
                "rollback_plan": "no guaranteed undo; verify and escalate if degraded",
            }
        ],
        "metrics_evidence": [{"summary": "error_rate=0.05"}],
        "logs_evidence": [],
        "traces_evidence": [],
        "deployment_evidence": [],
        "k8s_evidence": [],
        "db_evidence": [],
        "approval_status": {},
        "approval_decision": "",
        "execution_results": [],
        "verify_result": "",
        "verify_evidence": [],
        "verify_gates": [],
        "_verify_cycles": 0,
        "pre_action_snapshot": {},
        "_interrupts_enabled": False,
        "phase": "guardrail_checked",
    }

    state = human_approval(state, deps)
    assert state["phase"] == "approval_approved"
    state = take_snapshot(state, deps)
    assert state["pre_action_snapshot"]["k8s"]["replicas"] == 2
    state = execute_action(state, deps)
    assert backend.execute_calls
    state = verify(state, deps)
    assert state["verify_result"] == "degraded"
    gates = {gate["gate"]: gate for gate in state["verify_gates"]}
    assert gates["k8s_rollout"]["required"] is True
    assert gates["k8s_rollout"]["verdict"] == "degraded"

    state["root_cause"] = {"summary": "bad rollout", "confidence": 0.8}
    state = plan_actions(state, deps)

    assert state["recommended_actions"][0]["type"] == "rollback_release"
    assert "Pre-action snapshot for rollback planning" in llm.prompt
    assert "rollback" in llm.prompt.lower()


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
