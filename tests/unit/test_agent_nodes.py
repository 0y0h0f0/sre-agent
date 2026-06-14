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


def _seed_incident_run(db: Session, incident_id: str, agent_run_id: str) -> None:
    from packages.common.time import utc_now
    from packages.db.models import AgentRun, Incident

    db.add(
        Incident(
            incident_id=incident_id,
            fingerprint=f"fp_{incident_id}",
            source="mock",
            service="checkout",
            severity="P2",
            alert_name="DatabaseConnectionExhaustion",
            status="diagnosing",
            starts_at=utc_now(),
            labels={},
            annotations={},
        )
    )
    db.add(AgentRun(agent_run_id=agent_run_id, incident_id=incident_id, status="running"))
    db.flush()


class TestPersistEvidence:
    def test_persist_evidence_writes_ids_back_to_state(self, db_session: Session) -> None:
        from packages.agent.nodes._persist import persist_evidence
        from packages.common.time import utc_now
        from packages.db.models import AgentRun, Incident
        from packages.db.repositories.evidence_items import EvidenceItemRepository

        db_session.add(
            Incident(
                incident_id="inc_evidence",
                fingerprint="fp_evidence",
                source="mock",
                service="checkout",
                severity="P2",
                alert_name="High5xxAfterDeploy",
                status="diagnosing",
                starts_at=utc_now(),
                labels={},
                annotations={},
            )
        )
        db_session.add(
            AgentRun(
                agent_run_id="run_evidence",
                incident_id="inc_evidence",
                status="running",
            )
        )
        db_session.flush()
        evidence = [
            {
                "type": "metric",
                "source": "prometheus",
                "summary": "error rate elevated",
            }
        ]

        persisted = persist_evidence(db_session, "inc_evidence", "run_evidence", evidence)

        assert persisted[0] is evidence[0]
        assert evidence[0]["evidence_id"].startswith("evi_")
        row = EvidenceItemRepository(db_session).list_for_run("run_evidence")[0]
        assert row.evidence_id == evidence[0]["evidence_id"]
        assert row.payload["evidence_id"] == evidence[0]["evidence_id"]


class TestCollectK8sAndDbNodes:
    def test_collect_k8s_noops_without_tool(self, db_session: Session) -> None:
        from packages.agent.nodes.collect_k8s import collect_k8s

        deps = _make_deps(db_session)  # no k8s_tool -> None
        result = collect_k8s(_base_state(service_name="checkout"), deps)
        assert result["k8s_evidence"] == []
        assert result["phase"] == "k8s_collected"

    def test_collect_db_noops_without_tool(self, db_session: Session) -> None:
        from packages.agent.nodes.collect_db import collect_db

        deps = _make_deps(db_session)  # no db_diagnostics_tool -> None
        result = collect_db(_base_state(), deps)
        assert result["db_evidence"] == []
        assert result["phase"] == "db_collected"

    def test_collect_k8s_skips_when_alert_not_relevant(self, db_session: Session) -> None:
        from packages.agent.nodes.collect_k8s import collect_k8s
        from packages.tools.k8s import K8sDiagnosticsTool

        deps = _make_deps(db_session)
        deps.k8s_tool = K8sDiagnosticsTool()  # tool present, but alert is irrelevant
        state = _base_state(
            service_name="checkout", alert_name="RedisCacheAvalanche", severity="P2"
        )
        result = collect_k8s(state, deps)
        assert result["k8s_evidence"] == []  # gated out, no misleading pod state

    def test_collect_db_skips_when_alert_not_relevant(self, db_session: Session) -> None:
        from packages.agent.nodes.collect_db import collect_db
        from packages.tools.db_diagnostics import DbDiagnosticsTool

        deps = _make_deps(db_session)
        deps.db_diagnostics_tool = DbDiagnosticsTool()
        state = _base_state(alert_name="CertificateExpiry", severity="P3")
        result = collect_db(state, deps)
        assert result["db_evidence"] == []

    def test_collect_k8s_collects_fixture_evidence(self, db_session: Session) -> None:
        from packages.agent.nodes.collect_k8s import collect_k8s
        from packages.common.time import utc_now
        from packages.db.models import AgentRun, Incident
        from packages.tools.k8s import K8sDiagnosticsTool

        db_session.add(
            Incident(
                incident_id="inc_k8s",
                fingerprint="fp_k8s",
                source="mock",
                service="checkout",
                severity="P2",
                alert_name="PodRestartLoop",
                status="diagnosing",
                starts_at=utc_now(),
                labels={},
                annotations={},
            )
        )
        db_session.add(AgentRun(agent_run_id="run_k8s", incident_id="inc_k8s", status="running"))
        db_session.flush()

        deps = _make_deps(db_session)
        deps.k8s_tool = K8sDiagnosticsTool()  # fixture backend
        state = _base_state(
            incident_id="inc_k8s",
            agent_run_id="run_k8s",
            service_name="checkout",
            alert_name="PodRestartLoop",
            severity="P2",
        )
        result = collect_k8s(state, deps)

        assert result["phase"] == "k8s_collected"
        assert result["k8s_evidence"]

    def test_collect_db_collects_fixture_evidence(self, db_session: Session) -> None:
        from packages.agent.nodes.collect_db import collect_db
        from packages.common.time import utc_now
        from packages.db.models import AgentRun, Incident
        from packages.tools.db_diagnostics import DbDiagnosticsTool

        db_session.add(
            Incident(
                incident_id="inc_db",
                fingerprint="fp_db",
                source="mock",
                service="checkout",
                severity="P2",
                alert_name="DatabaseConnectionExhaustion",
                status="diagnosing",
                starts_at=utc_now(),
                labels={},
                annotations={},
            )
        )
        db_session.add(AgentRun(agent_run_id="run_db", incident_id="inc_db", status="running"))
        db_session.flush()

        deps = _make_deps(db_session)
        deps.db_diagnostics_tool = DbDiagnosticsTool()  # fixture backend
        state = _base_state(
            incident_id="inc_db",
            agent_run_id="run_db",
            alert_name="DatabaseConnectionExhaustion",
            severity="P2",
        )
        result = collect_db(state, deps)

        assert result["phase"] == "db_collected"
        assert result["db_evidence"]


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


class TestPlanActionsNode:
    def test_fallback_actions_are_copied_per_run(self, db_session: Session) -> None:
        from packages.agent.nodes.plan_actions import plan_actions
        from packages.agent.rules_fallback import _ACTIONS_MAP

        class FailingLLM:
            def generate_json(self, prompt, output_schema, *, thinking=False):
                raise RuntimeError("llm unavailable")

        deps = _make_deps(db_session)
        deps.llm = FailingLLM()
        state = _base_state(
            alert_name="DatabaseConnectionExhaustion",
            root_cause={"summary": "db pool exhausted", "confidence": 0.8},
        )

        first = plan_actions(state, deps)
        first["recommended_actions"][0]["action_id"] = "act_stale"
        second = plan_actions(state, deps)

        assert "action_id" not in _ACTIONS_MAP["DatabaseConnectionExhaustion"][0]
        assert "action_id" not in second["recommended_actions"][0]


class TestExecuteActionNode:
    def test_persists_automatic_actions_before_execution(self, db_session: Session) -> None:
        from packages.db.repositories.actions import ActionRepository

        incident_id = "inc_auto_actions"
        agent_run_id = "run_auto_actions"
        _seed_incident_run(db_session, incident_id, agent_run_id)

        deps = _make_deps(db_session)
        state = _base_state(
            incident_id=incident_id,
            agent_run_id=agent_run_id,
            recommended_actions=[
                {
                    "type": "adjust_connection_pool",
                    "target": "database",
                    "params": {"max_connections": 200},
                    "reason": "pool saturated",
                    "risk_level": "L1",
                    "allowed": True,
                    "requires_approval": False,
                },
                {
                    "type": "create_ticket",
                    "target": "db-team",
                    "params": {"priority": "P1"},
                    "reason": "track slow query cleanup",
                    "risk_level": "L1",
                    "allowed": True,
                    "requires_approval": False,
                },
            ],
        )

        result = execute_action(state, deps)

        rows = ActionRepository(db_session).list_for_run(agent_run_id)
        assert [row.type for row in rows] == ["adjust_connection_pool", "create_ticket"]
        assert {row.status for row in rows} == {"succeeded"}
        assert all(row.execution_result is not None for row in rows)
        assert all(action.get("action_id") for action in result["recommended_actions"])
        assert [item["action_id"] for item in result["execution_results"]] == [
            row.action_id for row in rows
        ]

    def test_uses_existing_action_id_without_duplicate(self, db_session: Session) -> None:
        from packages.db.repositories.actions import ActionRepository

        incident_id = "inc_existing_action"
        agent_run_id = "run_existing_action"
        _seed_incident_run(db_session, incident_id, agent_run_id)
        repo = ActionRepository(db_session)
        existing = repo.create(
            incident_id=incident_id,
            agent_run_id=agent_run_id,
            type="restart_pod",
            risk_level="L2",
            status="approved",
            target="checkout",
            params={},
            reason="approved by human",
        )
        db_session.flush()

        deps = _make_deps(db_session)
        result = execute_action(
            _base_state(
                incident_id=incident_id,
                agent_run_id=agent_run_id,
                recommended_actions=[
                    {
                        "action_id": existing.action_id,
                        "type": "restart_pod",
                        "target": "checkout",
                        "params": {},
                        "reason": "approved by human",
                        "risk_level": "L2",
                        "allowed": True,
                        "requires_approval": False,
                    }
                ],
            ),
            deps,
        )

        rows = repo.list_for_run(agent_run_id)
        assert len(rows) == 1
        assert rows[0].action_id == existing.action_id
        assert rows[0].status == "succeeded"
        assert result["execution_results"][0]["action_id"] == existing.action_id

    def test_stale_automatic_action_id_creates_current_run_action(
        self, db_session: Session
    ) -> None:
        from packages.db.repositories.actions import ActionRepository

        stale_incident_id = "inc_stale_previous"
        stale_run_id = "run_stale_previous"
        incident_id = "inc_stale_current"
        agent_run_id = "run_stale_current"
        _seed_incident_run(db_session, stale_incident_id, stale_run_id)
        _seed_incident_run(db_session, incident_id, agent_run_id)
        repo = ActionRepository(db_session)
        stale = repo.create(
            incident_id=stale_incident_id,
            agent_run_id=stale_run_id,
            type="adjust_connection_pool",
            risk_level="L1",
            status="succeeded",
            target="database",
            params={},
            reason="previous run",
        )
        db_session.flush()

        deps = _make_deps(db_session)
        result = execute_action(
            _base_state(
                incident_id=incident_id,
                agent_run_id=agent_run_id,
                recommended_actions=[
                    {
                        "action_id": stale.action_id,
                        "type": "adjust_connection_pool",
                        "target": "database",
                        "params": {"max_connections": 200},
                        "reason": "current run",
                        "risk_level": "L1",
                        "allowed": True,
                        "requires_approval": False,
                    }
                ],
            ),
            deps,
        )

        current_rows = list(repo.list_for_run(agent_run_id))
        assert len(current_rows) == 1
        assert current_rows[0].action_id != stale.action_id
        assert current_rows[0].status == "succeeded"
        assert result["execution_results"][0]["action_id"] == current_rows[0].action_id
        assert repo.get_by_public_id(stale.action_id).agent_run_id == stale_run_id

    def test_stale_approval_gated_action_id_fails_closed(self, db_session: Session) -> None:
        from packages.db.repositories.actions import ActionRepository

        stale_incident_id = "inc_stale_l2_previous"
        stale_run_id = "run_stale_l2_previous"
        incident_id = "inc_stale_l2_current"
        agent_run_id = "run_stale_l2_current"
        _seed_incident_run(db_session, stale_incident_id, stale_run_id)
        _seed_incident_run(db_session, incident_id, agent_run_id)
        repo = ActionRepository(db_session)
        stale = repo.create(
            incident_id=stale_incident_id,
            agent_run_id=stale_run_id,
            type="restart_pod",
            risk_level="L2",
            status="approved",
            target="checkout",
            params={},
            reason="previous approval",
        )
        db_session.flush()

        deps = _make_deps(db_session)
        result = execute_action(
            _base_state(
                incident_id=incident_id,
                agent_run_id=agent_run_id,
                recommended_actions=[
                    {
                        "action_id": stale.action_id,
                        "type": "restart_pod",
                        "target": "checkout",
                        "params": {},
                        "reason": "stale approval must not carry over",
                        "risk_level": "L2",
                        "allowed": True,
                        "requires_approval": False,
                    }
                ],
            ),
            deps,
        )

        assert result["errors"]
        assert "does not belong to current run" in result["errors"][0]["error"]
        assert list(repo.list_for_run(agent_run_id)) == []

    def test_stale_approval_gated_action_id_does_not_leave_automatic_row(
        self, db_session: Session
    ) -> None:
        from packages.db.repositories.actions import ActionRepository

        stale_incident_id = "inc_mixed_stale_previous"
        stale_run_id = "run_mixed_stale_previous"
        incident_id = "inc_mixed_stale_current"
        agent_run_id = "run_mixed_stale_current"
        _seed_incident_run(db_session, stale_incident_id, stale_run_id)
        _seed_incident_run(db_session, incident_id, agent_run_id)
        repo = ActionRepository(db_session)
        stale = repo.create(
            incident_id=stale_incident_id,
            agent_run_id=stale_run_id,
            type="restart_pod",
            risk_level="L2",
            status="approved",
            target="checkout",
            params={},
            reason="previous approval",
        )
        db_session.flush()

        deps = _make_deps(db_session)
        result = execute_action(
            _base_state(
                incident_id=incident_id,
                agent_run_id=agent_run_id,
                recommended_actions=[
                    {
                        "type": "create_ticket",
                        "target": "db-team",
                        "params": {},
                        "reason": "auto action should not be created",
                        "risk_level": "L1",
                        "allowed": True,
                        "requires_approval": False,
                    },
                    {
                        "action_id": stale.action_id,
                        "type": "restart_pod",
                        "target": "checkout",
                        "params": {},
                        "reason": "stale approval must fail closed",
                        "risk_level": "L2",
                        "allowed": True,
                        "requires_approval": False,
                    },
                ],
            ),
            deps,
        )

        assert result["errors"]
        assert "does not belong to current run" in result["errors"][0]["error"]
        assert list(repo.list_for_run(agent_run_id)) == []

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


class TestSnapshotVerifyRollbackFlow:
    def test_verify_records_tool_calls_with_current_run_id(
        self, db_session: Session, monkeypatch
    ) -> None:
        from packages.agent.nodes.verify import verify
        from packages.common.time import utc_now
        from packages.db.models import AgentRun, Incident
        from packages.tools.base import ToolResult

        db_session.add(
            Incident(
                incident_id="inc_verify",
                fingerprint="fp_verify",
                source="mock",
                service="checkout",
                severity="P1",
                alert_name="High5xxAfterDeploy",
                status="diagnosing",
                starts_at=utc_now(),
                labels={},
                annotations={},
            )
        )
        db_session.add(
            AgentRun(
                agent_run_id="run_verify",
                incident_id="inc_verify",
                status="running",
            )
        )
        db_session.flush()

        class StaticTool:
            def __init__(self, name: str, result: ToolResult) -> None:
                self.name = name
                self.timeout_seconds = 1.0
                self.result = result

            def run(self, query):
                return self.result

        calls = []
        deps = _make_deps(db_session)
        deps.metrics_tool = StaticTool(
            "metrics",
            ToolResult(
                status="succeeded",
                data={},
                summary="error_rate=0.005",
                evidence=[
                    {
                        "type": "metric",
                        "source": "prometheus",
                        "summary": "error_rate=0.005",
                    }
                ],
                duration_ms=1,
            ),
        )
        deps.logs_tool = StaticTool(
            "logs",
            ToolResult(status="succeeded", data={}, summary="clean", evidence=[], duration_ms=1),
        )
        deps.tool_call_recorder = lambda **kw: calls.append(kw)
        monkeypatch.setattr("packages.agent.nodes.verify.time.sleep", lambda _: None)

        result = verify(
            _base_state(
                incident_id="inc_verify",
                agent_run_id="run_verify",
                service_name="checkout",
                alert_name="High5xxAfterDeploy",
                metrics_evidence=[{"summary": "error_rate=0.05"}],
                execution_results=[{"risk_level": "L2", "type": "restart_pod"}],
                _verify_cycles=0,
            ),
            deps,
        )

        assert result["verify_result"] == "resolved"
        assert calls
        assert {call["agent_run_id"] for call in calls} == {"run_verify"}

    def test_take_snapshot_uses_executor_namespace_for_k8s_snapshot(
        self, db_session: Session
    ) -> None:
        from packages.agent.nodes.take_snapshot import take_snapshot
        from packages.tools.base import ToolResult

        class K8sTool:
            name = "k8s"
            timeout_seconds = 1.0

            def __init__(self) -> None:
                self.query = None

            def run(self, query):
                self.query = query
                return ToolResult(
                    status="succeeded",
                    data={
                        "payload": {
                            "revision": "7",
                            "replicas": 3,
                            "namespace": query.namespace,
                        }
                    },
                    summary="deployment snapshot",
                    duration_ms=1,
                )

        deps = _make_deps(db_session)
        deps.settings.executor_k8s_namespace = "payments"
        tool = K8sTool()
        deps.k8s_tool = tool

        result = take_snapshot(
            _base_state(
                service_name="checkout",
                recommended_actions=[{"type": "scale_deployment", "target": "checkout"}],
            ),
            deps,
        )

        assert tool.query is not None
        assert tool.query.namespace == "payments"
        assert result["pre_action_snapshot"]["k8s"]["revision"] == "7"

    def test_take_snapshot_preserves_original_snapshot_for_degraded_rollback(
        self, db_session: Session
    ) -> None:
        from packages.agent.nodes.take_snapshot import take_snapshot

        deps = _make_deps(db_session)
        deps.k8s_tool = MagicMock(side_effect=AssertionError("should not re-snapshot"))
        original_snapshot = {"taken_at": "before", "k8s": {"revision": "5", "replicas": 2}}

        result = take_snapshot(
            _base_state(
                verify_result="degraded",
                pre_action_snapshot=original_snapshot,
                recommended_actions=[{"type": "scale_back", "target": "checkout"}],
            ),
            deps,
        )

        assert result["phase"] == "snapshot_preserved"
        assert result["pre_action_snapshot"] is original_snapshot
        deps.k8s_tool.run.assert_not_called()

    def test_execute_action_passes_namespace_to_backend(self, db_session: Session) -> None:
        from packages.tools.executor_backends import ExecutionResult

        class RecordingBackend:
            name = "recording"

            def __init__(self) -> None:
                self.contexts = []

            def execute(self, action, context):
                self.contexts.append(context)
                return ExecutionResult(status="succeeded", message="ok")

            def rollback(self, action, snapshot, context):
                raise AssertionError("rollback not expected")

        deps = _make_deps(db_session)
        deps.settings.executor_k8s_namespace = "payments"
        backend = RecordingBackend()
        deps.executor_backend = backend

        execute_action(
            _base_state(
                service_name="checkout",
                recommended_actions=[
                    {"type": "create_ticket", "allowed": True, "requires_approval": False}
                ],
            ),
            deps,
        )

        assert backend.contexts[0].namespace == "payments"

    def test_degraded_rollback_action_uses_backend_rollback_with_snapshot(
        self, db_session: Session
    ) -> None:
        from packages.tools.executor_backends import ExecutionResult

        class RecordingBackend:
            name = "recording"

            def __init__(self) -> None:
                self.execute_calls = []
                self.rollback_calls = []

            def execute(self, action, context):
                self.execute_calls.append((action, context))
                return ExecutionResult(status="succeeded", message="execute")

            def rollback(self, action, snapshot, context):
                self.rollback_calls.append((action, snapshot, context))
                return ExecutionResult(status="succeeded", message="rollback")

        deps = _make_deps(db_session)
        backend = RecordingBackend()
        deps.executor_backend = backend
        snapshot = {"k8s": {"revision": "5", "replicas": 2}}

        result = execute_action(
            _base_state(
                verify_result="degraded",
                pre_action_snapshot=snapshot,
                recommended_actions=[
                    {
                        "type": "scale_back",
                        "target": "checkout",
                        "risk_level": "L2",
                        "allowed": True,
                        "requires_approval": False,
                    }
                ],
            ),
            deps,
        )

        assert not backend.execute_calls
        assert backend.rollback_calls[0][1] == snapshot
        assert result["execution_results"][0]["execution_result"]["message"] == "rollback"

    def test_degraded_rollback_deployment_alias_uses_backend_rollback(
        self, db_session: Session
    ) -> None:
        from packages.tools.executor_backends import ExecutionResult

        class RecordingBackend:
            name = "recording"

            def __init__(self) -> None:
                self.execute_calls = []
                self.rollback_calls = []

            def execute(self, action, context):
                self.execute_calls.append((action, context))
                return ExecutionResult(status="succeeded", message="execute")

            def rollback(self, action, snapshot, context):
                self.rollback_calls.append((action, snapshot, context))
                return ExecutionResult(status="succeeded", message="rollback")

        deps = _make_deps(db_session)
        backend = RecordingBackend()
        deps.executor_backend = backend
        snapshot = {"k8s": {"revision": "5"}}

        result = execute_action(
            _base_state(
                verify_result="degraded",
                pre_action_snapshot=snapshot,
                recommended_actions=[
                    {
                        "type": "rollback_deployment",
                        "target": "checkout",
                        "risk_level": "L3",
                        "allowed": True,
                        "requires_approval": False,
                    }
                ],
            ),
            deps,
        )

        assert not backend.execute_calls
        assert backend.rollback_calls[0][0]["type"] == "rollback_deployment"
        assert backend.rollback_calls[0][1] == snapshot
        assert result["execution_results"][0]["execution_result"]["message"] == "rollback"

    def test_plan_actions_includes_degraded_snapshot_context(self, db_session: Session) -> None:
        from packages.agent.nodes.plan_actions import plan_actions
        from packages.agent.schemas import PlannedAction

        class CapturingLLM:
            def __init__(self) -> None:
                self.prompt = ""

            def generate_json(self, prompt, output_schema, *, thinking=False):
                self.prompt = prompt
                return [
                    PlannedAction(
                        type="scale_back",
                        target="checkout",
                        params={"replicas": 2},
                        reason="restore previous replica count",
                        risk_hint="L2",
                        rollback_plan="scale up again if needed",
                    )
                ]

        deps = _make_deps(db_session)
        llm = CapturingLLM()
        deps.llm = llm

        plan_actions(
            _base_state(
                alert_name="High5xxAfterDeploy",
                root_cause={"summary": "bad scale", "confidence": 0.8},
                verify_result="degraded",
                verify_evidence=[{"summary": "error_rate=0.20"}],
                pre_action_snapshot={
                    "taken_at": "2026-06-01T00:00:00Z",
                    "action_types": ["scale_deployment"],
                    "k8s": {"revision": "5", "replicas": 2, "image": "checkout:v1"},
                    "metrics_evidence": [{"summary": "error_rate=0.05"}],
                    "logs_evidence": [],
                    "traces_evidence": [],
                },
            ),
            deps,
        )

        assert "Pre-action snapshot for rollback planning" in llm.prompt
        assert "revision=5" in llm.prompt
        assert "replicas=2" in llm.prompt
        assert "scale_back" in llm.prompt


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
