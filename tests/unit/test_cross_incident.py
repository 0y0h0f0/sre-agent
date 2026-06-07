"""Unit tests for the cross_incident LangGraph node (Phase 5.1)."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from packages.agent.nodes.cross_incident import cross_incident
from packages.agent.schemas import AgentDeps
from packages.agent.state import IncidentState
from packages.common.settings import Settings
from packages.db.models import Incident


def _settings() -> Settings:
    return Settings(
        database_url="sqlite+pysqlite:///:memory:",
        cross_incident_max_results=5,
    )


def _create_incident(
    db: Session,
    incident_id: str = "inc_test",
    fingerprint: str = "fp-test",
    service: str = "checkout",
    alert_name: str = "TestAlert",
    root_cause: str | None = "CPU saturation",
) -> Incident:
    incident = Incident(
        incident_id=incident_id,
        fingerprint=fingerprint,
        source="mock",
        service=service,
        severity="P2",
        alert_name=alert_name,
        status="resolved",
        starts_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
        root_cause_summary=root_cause,
        labels={},
        annotations={},
        raw_payload={},
    )
    db.add(incident)
    return incident


def _deps(db: Session) -> AgentDeps:
    return AgentDeps(
        db=db,
        settings=_settings(),
        tool_cache=None,  # type: ignore[arg-type]
        metrics_tool=None,  # type: ignore[arg-type]
        logs_tool=None,  # type: ignore[arg-type]
        trace_tool=None,  # type: ignore[arg-type]
        git_change_tool=None,  # type: ignore[arg-type]
        runbook_search_tool=None,  # type: ignore[arg-type]
        k8s_tool=None,  # type: ignore[arg-type]
        db_diagnostics_tool=None,  # type: ignore[arg-type]
        memory_store=None,  # type: ignore[arg-type]
        context_builder=None,  # type: ignore[arg-type]
        llm=None,  # type: ignore[arg-type]
        node_tracer=lambda **kwargs: None,  # type: ignore[arg-type]
        tool_call_recorder=lambda **kwargs: None,  # type: ignore[arg-type]
    )


class TestCrossIncidentNode:
    def test_empty_state_when_no_incident_id(self, db_session: Session) -> None:
        db_session.commit()
        state: IncidentState = {}
        deps = _deps(db_session)
        result = cross_incident(state, deps)

        assert result.get("cross_incident_context") == []

    def test_empty_state_when_no_service_name(self, db_session: Session) -> None:
        db_session.commit()
        state: IncidentState = {"incident_id": "inc_test"}
        deps = _deps(db_session)
        result = cross_incident(state, deps)

        assert result.get("cross_incident_context") == []

    def test_finds_same_fingerprint_incidents(self, db_session: Session) -> None:
        db_session.commit()
        _create_incident(db_session, "inc_test", fingerprint="fp-x", service="checkout")
        _create_incident(db_session, "inc_related", fingerprint="fp-x", service="checkout",
                         alert_name="Related", root_cause="Same fingerprint match")
        db_session.commit()

        state: IncidentState = {
            "incident_id": "inc_test",
            "service_name": "checkout",
            "alert_payload": {"fingerprint": "fp-x"},
        }
        deps = _deps(db_session)
        result = cross_incident(state, deps)

        ctx = result.get("cross_incident_context", [])
        assert len(ctx) == 1
        assert ctx[0]["incident_id"] == "inc_related"
        assert ctx[0]["correlation_type"] == "same_fingerprint"

    def test_finds_same_service_incidents(self, db_session: Session) -> None:
        db_session.commit()
        _create_incident(db_session, "inc_test", fingerprint="fp-x", service="checkout")
        _create_incident(db_session, "inc_samesvc", fingerprint="fp-y", service="checkout",
                         alert_name="Other", root_cause="Different fingerprint, same service")
        db_session.commit()

        state: IncidentState = {
            "incident_id": "inc_test",
            "service_name": "checkout",
            "alert_payload": {"fingerprint": "fp-x"},
        }
        deps = _deps(db_session)
        result = cross_incident(state, deps)

        ctx = result.get("cross_incident_context", [])
        assert len(ctx) == 1
        assert ctx[0]["incident_id"] == "inc_samesvc"
        assert ctx[0]["correlation_type"] == "similar_service"

    def test_respects_max_results(self, db_session: Session) -> None:
        db_session.commit()
        _create_incident(db_session, "inc_test", fingerprint="fp-x", service="checkout")
        for i in range(10):
            _create_incident(db_session, f"inc_{i}", fingerprint=f"fp-{i}", service="checkout",
                             alert_name=f"Alert{i}", root_cause="Some cause")
        db_session.commit()

        state: IncidentState = {
            "incident_id": "inc_test",
            "service_name": "checkout",
            "alert_payload": {"fingerprint": "fp-x"},
        }
        deps = _deps(db_session)
        result = cross_incident(state, deps)

        ctx = result.get("cross_incident_context", [])
        assert len(ctx) <= 5
