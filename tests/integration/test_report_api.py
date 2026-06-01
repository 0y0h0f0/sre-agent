from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from packages.common.ids import new_id
from packages.db.models import Action, AgentRun, EvidenceItem, Incident


def _create_incident(db: Session) -> Incident:
    incident = Incident(
        incident_id=new_id("inc_"),
        fingerprint=f"fp-{new_id('fp_')}",
        source="mock",
        service="checkout-api",
        severity="P2",
        alert_name="High5xxAfterDeploy",
        status="resolved",
        starts_at=datetime(2026, 6, 1, 0, 0, tzinfo=UTC),
        labels={"team": "payments"},
        annotations={"summary": "5xx increased after deploy"},
        root_cause_summary="Deployment v2026.06.01 introduced downstream timeout errors",
    )
    db.add(incident)
    return incident


def _create_run(db: Session, incident_id: str) -> AgentRun:
    run = AgentRun(
        agent_run_id=new_id("run_"),
        incident_id=incident_id,
        status="succeeded",
        model_name="fake-diagnosis-model",
        prompt_version="v1",
        state={
            "incident_report": {
                "root_cause": "Bad release caused elevated 5xx",
                "impact": "Checkout requests failed for a subset of users",
                "timeline": [{"time": "2026-06-01T00:00:00Z", "event": "Alert fired"}],
                "actions": [{"type": "rollback_release", "status": "approved"}],
                "follow_ups": [{"item": "Add deploy canary checks", "status": "open"}],
            }
        },
        checkpoint_thread_id="run-thread",
        checkpoint_ns="",
    )
    db.add(run)
    return run


def _create_evidence(db: Session, incident_id: str, agent_run_id: str) -> EvidenceItem:
    evidence = EvidenceItem(
        evidence_id=new_id("evd_"),
        incident_id=incident_id,
        agent_run_id=agent_run_id,
        type="logs",
        source="loki",
        title="5xx errors after deploy",
        excerpt="timeout calling payment-api",
        payload={},
        confidence=0.9,
        timestamp=datetime(2026, 6, 1, 0, 3, tzinfo=UTC),
    )
    db.add(evidence)
    return evidence


def _create_action(db: Session, incident_id: str, agent_run_id: str) -> Action:
    action = Action(
        action_id=new_id("act_"),
        incident_id=incident_id,
        agent_run_id=agent_run_id,
        type="rollback_release",
        risk_level="L3",
        status="approved",
        executor="mock",
        target="checkout-api",
        params={},
        reason="new release correlated with 5xx spike",
        rollback_plan="redeploy previous stable version",
    )
    db.add(action)
    return action


def test_report_regenerate_creates_versions_and_gets_latest(
    client: TestClient,
    db_session: Session,
) -> None:
    incident = _create_incident(db_session)
    run = _create_run(db_session, incident.incident_id)
    evidence = _create_evidence(db_session, incident.incident_id, run.agent_run_id)
    _create_action(db_session, incident.incident_id, run.agent_run_id)
    db_session.commit()

    missing = client.get(f"/api/incidents/{incident.incident_id}/report")
    assert missing.status_code == 404

    first = client.post(f"/api/incidents/{incident.incident_id}/report/regenerate")
    assert first.status_code == 201
    first_body = first.json()
    assert first_body["version"] == 1
    assert first_body["root_cause"] == "Bad release caused elevated 5xx"
    assert evidence.evidence_id in first_body["evidence_ids"]

    latest = client.get(f"/api/incidents/{incident.incident_id}/report")
    assert latest.status_code == 200
    assert latest.json()["report_id"] == first_body["report_id"]

    second = client.post(f"/api/incidents/{incident.incident_id}/report/regenerate")
    assert second.status_code == 201
    second_body = second.json()
    assert second_body["version"] == 2
    assert second_body["report_id"] != first_body["report_id"]


def test_report_regenerate_requires_incident_run(
    client: TestClient,
    db_session: Session,
) -> None:
    incident = _create_incident(db_session)
    db_session.commit()

    response = client.post(f"/api/incidents/{incident.incident_id}/report/regenerate")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "CONFLICT"
