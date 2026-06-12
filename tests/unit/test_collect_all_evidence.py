"""Unit tests for the parallel evidence collection orchestrator."""

from __future__ import annotations

from unittest.mock import MagicMock

from sqlalchemy.orm import Session

from packages.agent.nodes.collect_all_evidence import collect_all_evidence
from packages.agent.schemas import AgentDeps
from packages.agent.state import IncidentState
from packages.common.settings import Settings
from packages.common.time import utc_now
from packages.db.models import AgentRun, Incident
from packages.memory.context_builder import ContextBuilder
from packages.memory.memory_store import MemoryStore
from packages.tools.base import ToolResult
from packages.tools.cache import RequestLocalToolCache


def _fake_tool(tool_name: str):
    """Return a tool that succeeds with a stub evidence item."""

    class FT:
        name: str = tool_name

        def run(self, query):
            return ToolResult(
                status="succeeded",
                data={},
                summary=f"{tool_name} ok",
                evidence=[{
                    "type": tool_name,
                    "source": "mock",
                    "summary": f"test {tool_name} data",
                }],
                duration_ms=1,
            )

    return FT()


def _failing_tool(tool_name: str):
    """Return a tool that raises on every call."""

    class FT:
        name: str = tool_name

        def run(self, query):
            raise RuntimeError(f"{tool_name} unavailable")

    return FT()


def _make_deps(db: Session, **overrides) -> AgentDeps:
    settings = Settings(
        database_url="sqlite://",
        redis_url="memory://",
        celery_broker_url="memory://",
        celery_result_backend="memory://",
    )
    kwargs = dict(
        db=db,
        settings=settings,
        tool_cache=RequestLocalToolCache(),
        metrics_tool=_fake_tool("metrics"),
        logs_tool=_fake_tool("logs"),
        trace_tool=_fake_tool("traces"),
        git_change_tool=_fake_tool("git_changes"),
        runbook_search_tool=_fake_tool("runbook_search"),
        memory_store=MemoryStore(db),
        context_builder=ContextBuilder(),
        llm=MagicMock(),
        node_tracer=lambda **kw: None,
        tool_call_recorder=lambda **kw: None,
    )
    kwargs.update(overrides)
    return AgentDeps(**kwargs)


def _base_state(**overrides) -> IncidentState:
    state: IncidentState = {
        "incident_id": "inc_test",
        "agent_run_id": "run_test",
        "alert_payload": {},
        "service_name": "checkout",
        "alert_name": "High5xxAfterDeploy",
        "severity": "P1",
        "time_window": {
            "start": "2026-06-01T00:00:00+00:00",
            "end": "2026-06-01T01:00:00+00:00",
        },
        "metrics_evidence": [],
        "logs_evidence": [],
        "traces_evidence": [],
        "deployment_evidence": [],
        "k8s_evidence": [],
        "db_evidence": [],
        "runbook_context": [],
        "memory_context": [],
        "cross_incident_context": [],
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


class TestCollectAllEvidence:
    def test_all_six_evidence_sources_populated(self, db_session: Session) -> None:
        """After parallel collection, all 6 evidence keys are present in state."""
        db_session.add(
            Incident(
                incident_id="inc_test",
                fingerprint="fp_test",
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
            AgentRun(agent_run_id="run_test", incident_id="inc_test", status="running")
        )
        db_session.flush()

        deps = _make_deps(db_session)
        result = collect_all_evidence(_base_state(), deps)

        assert result["phase"] == "evidence_collected"
        assert "metrics_evidence" in result
        assert "logs_evidence" in result
        assert "traces_evidence" in result
        assert "deployment_evidence" in result
        assert "k8s_evidence" in result
        assert "db_evidence" in result

    def test_phase_set_to_evidence_collected(self, db_session: Session) -> None:
        """The orchestrator sets phase to 'evidence_collected', not per-collector phases."""
        db_session.add(
            Incident(
                incident_id="inc_test",
                fingerprint="fp_test",
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
            AgentRun(agent_run_id="run_test", incident_id="inc_test", status="running")
        )
        db_session.flush()

        deps = _make_deps(db_session)
        result = collect_all_evidence(_base_state(), deps)

        assert result["phase"] == "evidence_collected"

    def test_single_collector_failure_does_not_block_others(self, db_session: Session) -> None:
        """When one collector raises, the other 5 still complete normally."""
        db_session.add(
            Incident(
                incident_id="inc_test",
                fingerprint="fp_test",
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
            AgentRun(agent_run_id="run_test", incident_id="inc_test", status="running")
        )
        db_session.flush()

        deps = _make_deps(db_session, metrics_tool=_failing_tool("metrics"))
        result = collect_all_evidence(_base_state(), deps)

        # The error is recorded but other collectors should have run
        assert result["phase"] == "evidence_collected"
        errors = result.get("errors", [])
        assert any("collect_metrics" in str(e) for e in errors), (
            f"Expected metrics error in errors list: {errors}"
        )

    def test_evidence_ids_assigned_after_batch_persist(self, db_session: Session) -> None:
        """After collect_all_evidence, persisted evidence has evidence_id set."""
        from packages.db.repositories.evidence_items import EvidenceItemRepository

        db_session.add(
            Incident(
                incident_id="inc_test",
                fingerprint="fp_test",
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
            AgentRun(agent_run_id="run_test", incident_id="inc_test", status="running")
        )
        db_session.flush()

        deps = _make_deps(db_session)
        result = collect_all_evidence(_base_state(), deps)

        # Evidence items in state should have evidence_id assigned by batch persist
        for key in (
            "metrics_evidence",
            "logs_evidence",
            "traces_evidence",
            "deployment_evidence",
            "k8s_evidence",
            "db_evidence",
        ):
            items = result.get(key, [])
            for item in items:
                assert "evidence_id" in item, (
                    f"Missing evidence_id in {key} item: {item}"
                )
                assert item["evidence_id"].startswith("evi_"), (
                    f"Unexpected evidence_id format: {item['evidence_id']}"
                )

        # DB should have evidence rows — 4 active collectors × 1 item each
        rows = EvidenceItemRepository(db_session).list_for_run("run_test")
        assert len(rows) == 4, f"Expected 4 evidence rows, got {len(rows)}"

    def test_node_tracer_and_tool_recorder_replayed(self, db_session: Session) -> None:
        """Captured trace calls are replayed on the real callbacks after collection."""
        db_session.add(
            Incident(
                incident_id="inc_test",
                fingerprint="fp_test",
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
            AgentRun(agent_run_id="run_test", incident_id="inc_test", status="running")
        )
        db_session.flush()

        node_calls: list[dict] = []
        tool_calls: list[dict] = []

        deps = _make_deps(
            db_session,
            node_tracer=lambda **kw: node_calls.append(dict(kw)),
            tool_call_recorder=lambda **kw: tool_calls.append(dict(kw)),
        )
        collect_all_evidence(_base_state(), deps)

        # The orchestrator itself emits one node_tracer call
        assert len(node_calls) >= 1, f"Expected at least 1 node_tracer call, got {len(node_calls)}"
        # 4 of 6 collectors emit tool calls (k8s and db are no-ops without their
        # optional tools — deps.k8s_tool and deps.db_diagnostics_tool are None).
        assert len(tool_calls) == 4, f"Expected 4 tool_call_recorder calls, got {len(tool_calls)}"


class TestCollectAllEvidenceK8sDbOptional:
    def test_k8s_and_db_skipped_when_tools_absent(self, db_session: Session) -> None:
        """When k8s_tool and db_diagnostics_tool are None, they produce empty evidence."""
        db_session.add(
            Incident(
                incident_id="inc_test",
                fingerprint="fp_test",
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
            AgentRun(agent_run_id="run_test", incident_id="inc_test", status="running")
        )
        db_session.flush()

        deps = _make_deps(db_session)  # no k8s_tool, no db_diagnostics_tool
        result = collect_all_evidence(_base_state(), deps)

        assert result["k8s_evidence"] == []
        assert result["db_evidence"] == []
