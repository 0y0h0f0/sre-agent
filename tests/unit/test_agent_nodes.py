"""Unit tests for agent nodes — guardrail_check, human_approval, execute_action."""

from __future__ import annotations

from unittest.mock import MagicMock

from sqlalchemy.orm import Session

from packages.agent.nodes.execute_action import execute_action
from packages.agent.nodes.guardrail_check import guardrail_check
from packages.agent.nodes.human_approval import human_approval
from packages.agent.schemas import AgentDeps
from packages.agent.state import IncidentState
from packages.common.settings import Settings
from packages.memory.context_builder import ContextBuilder
from packages.memory.memory_store import MemoryStore
from packages.tools.cache import RequestLocalToolCache


def _make_deps(db: Session) -> AgentDeps:
    settings = Settings(
        database_url="sqlite://",
        redis_url="memory://",
        celery_broker_url="memory://",
        celery_result_backend="memory://",
    )
    return AgentDeps(
        db=db,
        settings=settings,
        tool_cache=RequestLocalToolCache(),
        metrics_tool=MagicMock(),
        logs_tool=MagicMock(),
        trace_tool=MagicMock(),
        git_change_tool=MagicMock(),
        runbook_search_tool=MagicMock(),
        memory_store=MemoryStore(db),
        context_builder=ContextBuilder(),
        llm=MagicMock(),
        node_tracer=lambda **kw: None,
        tool_call_recorder=lambda **kw: None,
    )


def _base_state(**overrides) -> IncidentState:
    state: IncidentState = {
        "incident_id": "inc_test",
        "agent_run_id": "run_test",
        "alert_payload": {},
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
    state.update(overrides)  # type: ignore[typeddict-unknown-key]
    return state


class TestGuardrailCheckNode:
    def test_classifies_l0_actions(self, db_session: Session) -> None:
        deps = _make_deps(db_session)
        state = _base_state(
            recommended_actions=[
                {"type": "query_metrics", "target": "", "params": {}},
                {"type": "query_logs", "target": "", "params": {}},
            ]
        )
        result = guardrail_check(state, deps)
        assert result["phase"] == "guardrail_checked"
        assert result["_needs_approval"] is False  # type: ignore[typeddict-unknown-key]
        for a in result["recommended_actions"]:
            assert a["risk_level"] == "L0"

    def test_detects_approval_needed(self, db_session: Session) -> None:
        deps = _make_deps(db_session)
        state = _base_state(
            recommended_actions=[
                {"type": "query_metrics", "target": "", "params": {}},
                {"type": "restart_pod", "target": "checkout", "params": {}},
            ]
        )
        result = guardrail_check(state, deps)
        assert result["_needs_approval"] is True  # type: ignore[typeddict-unknown-key]

    def test_detects_all_l4(self, db_session: Session) -> None:
        deps = _make_deps(db_session)
        state = _base_state(
            recommended_actions=[
                {"type": "delete_data", "target": "", "params": {}},
                {"type": "truncate_table", "target": "", "params": {}},
            ]
        )
        result = guardrail_check(state, deps)
        assert result["_all_l4"] is True  # type: ignore[typeddict-unknown-key]


class TestHumanApprovalNode:
    def test_skips_when_no_approval_needed(self, db_session: Session) -> None:
        deps = _make_deps(db_session)
        state = _base_state(
            recommended_actions=[
                {"type": "query_metrics", "target": "", "params": {}, "requires_approval": False},
            ]
        )
        result = human_approval(state, deps)
        assert result["phase"] == "approval_skipped"

    def test_creates_approval_and_interrupts(self, db_session: Session, monkeypatch) -> None:
        """First pass: creates DB records then raises GraphInterrupt."""
        from langgraph.errors import GraphInterrupt

        # Mock interrupt to simulate the pause
        interrupted = []

        def mock_interrupt(data):
            interrupted.append(data)
            raise GraphInterrupt("interrupted for approval")

        monkeypatch.setattr("packages.agent.nodes.human_approval.interrupt", mock_interrupt)

        deps = _make_deps(db_session)
        state = _base_state(
            incident_id="inc_01",
            agent_run_id="run_01",
            recommended_actions=[
                {
                    "type": "restart_pod",
                    "target": "checkout",
                    "params": {},
                    "requires_approval": True,
                    "risk_level": "L2",
                    "reason": "pod crash",
                },
            ],
        )

        # Should raise GraphInterrupt via mock_interrupt
        raised = False
        try:
            human_approval(state, deps)
        except GraphInterrupt:
            raised = True

        assert raised, "Should have raised GraphInterrupt"
        assert len(interrupted) == 1
        assert interrupted[0]["type"] == "approval_required"
        assert len(interrupted[0]["approval_ids"]) == 1


class TestExecuteActionNode:
    def test_executes_l0_l1_actions(self, db_session: Session) -> None:
        deps = _make_deps(db_session)
        state = _base_state(
            recommended_actions=[
                {
                    "type": "query_metrics",
                    "target": "",
                    "params": {},
                    "allowed": True,
                    "requires_approval": False,
                },
                {
                    "type": "create_ticket",
                    "target": "svc",
                    "params": {},
                    "allowed": True,
                    "requires_approval": False,
                },
            ]
        )
        result = execute_action(state, deps)
        assert result["phase"] == "actions_executed"
        assert len(result["execution_results"]) == 2
        for r in result["execution_results"]:
            assert r["execution_result"]["status"] == "succeeded"

    def test_skips_disallowed_actions(self, db_session: Session) -> None:
        deps = _make_deps(db_session)
        state = _base_state(
            recommended_actions=[
                {
                    "type": "delete_data",
                    "target": "",
                    "params": {},
                    "allowed": False,
                    "requires_approval": False,
                },
            ]
        )
        result = execute_action(state, deps)
        assert len(result["execution_results"]) == 0

    def test_skips_actions_awaiting_approval(self, db_session: Session) -> None:
        deps = _make_deps(db_session)
        state = _base_state(
            recommended_actions=[
                {
                    "type": "restart_pod",
                    "target": "checkout",
                    "params": {},
                    "allowed": True,
                    "requires_approval": True,
                },
            ]
        )
        result = execute_action(state, deps)
        assert len(result["execution_results"]) == 0

    def test_unknown_action_type_gets_mock_result(self, db_session: Session) -> None:
        deps = _make_deps(db_session)
        state = _base_state(
            recommended_actions=[
                {
                    "type": "unknown_action",
                    "target": "",
                    "params": {},
                    "allowed": True,
                    "requires_approval": False,
                },
            ]
        )
        result = execute_action(state, deps)
        assert len(result["execution_results"]) == 1
        assert result["execution_results"][0]["execution_result"]["status"] == "succeeded"
