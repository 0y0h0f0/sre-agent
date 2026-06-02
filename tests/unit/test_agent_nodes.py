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

    def test_auto_approve_never_approves_l3(self, db_session: Session) -> None:
        """No-checkpointer auto-approve must NOT approve L3 actions.

        L3 (rollback / rate-limit) requires a human second confirmation and may
        never be auto-approved. The L2 action is auto-approved; the L3 action
        stays ``waiting`` in the DB and keeps ``requires_approval`` set so
        ``execute_action`` skips it.
        """
        from packages.db.repositories.approvals import ApprovalRepository

        deps = _make_deps(db_session)
        state = _base_state(
            incident_id="inc_h1",
            agent_run_id="run_h1",
            _interrupts_enabled=False,
            recommended_actions=[
                {
                    "type": "restart_pod",
                    "target": "checkout",
                    "params": {},
                    "requires_approval": True,
                    "risk_level": "L2",
                    "reason": "pod crash",
                },
                {
                    "type": "rollback_release",
                    "target": "checkout",
                    "params": {},
                    "requires_approval": True,
                    "risk_level": "L3",
                    "reason": "bad deploy",
                },
            ],
        )

        result = human_approval(state, deps)
        assert result["phase"] == "approval_approved"

        approval_repo = ApprovalRepository(db_session)
        approvals = approval_repo.list_for_incident("inc_h1")
        by_action_risk = {a["action_id"]: a["risk_level"] for a in result["recommended_actions"]}
        statuses = {by_action_risk[a.action_id]: a.status for a in approvals}
        assert statuses["L2"] == "approved"
        assert statuses["L3"] == "waiting"

        actions_by_risk = {a["risk_level"]: a for a in result["recommended_actions"]}
        assert actions_by_risk["L2"]["requires_approval"] is False
        assert actions_by_risk["L3"]["requires_approval"] is True


class TestHumanApprovalResume:
    """Resume path must honor the actual per-approval DB decisions.

    Regression coverage for two bugs:
      1. A single approval must NOT auto-approve the whole batch.
      2. A rejection must clear approval_decision (no infinite replan loop).
    """

    def _seed(self, db: Session, *, statuses: list[str]):
        from packages.common.ids import new_id
        from packages.common.time import utc_now
        from packages.db.models import Action, AgentRun, Approval, Incident

        inc_id = new_id("inc_")
        run_id = new_id("run_")
        db.add(
            Incident(
                incident_id=inc_id,
                fingerprint=new_id("fp_"),
                source="mock",
                service="checkout",
                severity="P1",
                alert_name="HighErrorRate",
                status="diagnosing",
                starts_at=utc_now(),
                labels={},
                annotations={},
            )
        )
        db.add(AgentRun(agent_run_id=run_id, incident_id=inc_id, status="waiting_approval"))
        db.flush()

        actions: list[dict] = []
        approval_ids: list[str] = []
        for status in statuses:
            action_id = new_id("act_")
            db.add(
                Action(
                    action_id=action_id,
                    incident_id=inc_id,
                    agent_run_id=run_id,
                    type="restart_pod",
                    risk_level="L2",
                    status="waiting_approval",
                    target="checkout",
                    params={},
                    reason="r",
                )
            )
            apv_id = new_id("apv_")
            db.add(
                Approval(
                    approval_id=apv_id,
                    action_id=action_id,
                    incident_id=inc_id,
                    agent_run_id=run_id,
                    status=status,
                    requested_at=utc_now(),
                )
            )
            actions.append(
                {
                    "type": "restart_pod",
                    "target": "checkout",
                    "params": {},
                    "requires_approval": True,
                    "allowed": True,
                    "risk_level": "L2",
                    "action_id": action_id,
                }
            )
            approval_ids.append(apv_id)
        db.flush()
        return inc_id, run_id, actions, approval_ids

    def test_single_approval_does_not_approve_whole_batch(self, db_session: Session) -> None:
        inc_id, run_id, actions, approval_ids = self._seed(
            db_session, statuses=["approved", "waiting"]
        )
        deps = _make_deps(db_session)
        state = _base_state(
            incident_id=inc_id,
            agent_run_id=run_id,
            recommended_actions=actions,
            approval_status={"status": "waiting", "approval_ids": approval_ids},
            approval_decision="approved",
        )

        result = human_approval(state, deps)

        assert result["phase"] == "approval_approved"
        # The approved action is executable...
        assert result["recommended_actions"][0]["requires_approval"] is False
        # ...but the still-waiting one must NOT be silently approved.
        assert result["recommended_actions"][1]["requires_approval"] is True
        # execute_action only runs allowed + not-requires-approval actions.
        exec_result = execute_action(result, deps)
        assert len(exec_result["execution_results"]) == 1

    def test_rejection_clears_decision_and_counts_replan(self, db_session: Session) -> None:
        inc_id, run_id, actions, approval_ids = self._seed(
            db_session, statuses=["rejected", "rejected"]
        )
        deps = _make_deps(db_session)
        state = _base_state(
            incident_id=inc_id,
            agent_run_id=run_id,
            recommended_actions=actions,
            approval_status={"status": "waiting", "approval_ids": approval_ids},
            approval_decision="rejected",
            _replan_count=0,
        )

        result = human_approval(state, deps)

        assert result["phase"] == "approval_rejected"
        # Decision cleared so the replanned batch gets a fresh approval round
        # instead of re-reading a stale "rejected" and looping forever.
        assert result["approval_decision"] == ""
        assert result["_replan_count"] == 1
        for action in result["recommended_actions"]:
            assert action["allowed"] is False


class TestRouteAfterApproval:
    def test_replan_bounded_by_cap(self) -> None:
        from packages.agent.graph import _route_after_approval
        from packages.agent.nodes.human_approval import MAX_REPLAN_CYCLES

        below = _base_state(phase="approval_rejected", _replan_count=MAX_REPLAN_CYCLES - 1)
        assert _route_after_approval(below) == "replan"

        at_cap = _base_state(phase="approval_rejected", _replan_count=MAX_REPLAN_CYCLES)
        assert _route_after_approval(at_cap) == "report"

    def test_approved_routes_to_execute(self) -> None:
        from packages.agent.graph import _route_after_approval

        assert _route_after_approval(_base_state(phase="approval_approved")) == "execute"


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


class TestGenerateReportReviewFlag:
    """The dangling cross-validation flag must surface in the report (M4)."""

    def test_report_surfaces_needs_human_review(self, db_session: Session) -> None:
        from packages.agent.nodes.generate_report import generate_report

        deps = _make_deps(db_session)
        state = _base_state(
            incident_id="inc_m4",
            agent_run_id="run_m4",
            needs_human_review=True,
            root_cause={"summary": "pool exhausted", "confidence": 0.7},
        )
        result = generate_report(state, deps)
        report = result["incident_report"]
        assert report["needs_human_review"] is True
        assert any("review" in str(f).lower() for f in report.get("follow_ups", []))

    def test_report_omits_review_note_when_not_flagged(self, db_session: Session) -> None:
        from packages.agent.nodes.generate_report import generate_report

        deps = _make_deps(db_session)
        state = _base_state(
            incident_id="inc_m4b",
            agent_run_id="run_m4b",
            needs_human_review=False,
            root_cause={"summary": "pool exhausted", "confidence": 0.7},
        )
        result = generate_report(state, deps)
        assert result["incident_report"]["needs_human_review"] is False
