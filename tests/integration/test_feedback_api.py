"""Integration tests for Phase 5 feedback API endpoints."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from packages.db.models import Incident


def _create_incident(
    db: Session,
    incident_id: str = "inc_fbtest",
    fingerprint: str = "fp-fb",
    service: str = "checkout",
    alert_name: str = "TestAlert",
    severity: str = "P2",
    root_cause: str | None = "CPU saturation",
    status: str = "open",
) -> Incident:
    incident = Incident(
        incident_id=incident_id,
        fingerprint=fingerprint,
        source="mock",
        service=service,
        severity=severity,
        alert_name=alert_name,
        status=status,
        starts_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
        root_cause_summary=root_cause,
        labels={},
        annotations={},
        raw_payload={},
    )
    db.add(incident)
    return incident


class TestNFAMarkAPI:
    def test_mark_nfa_returns_201(self, client: TestClient, db_session: Session) -> None:
        db_session.commit()
        _create_incident(db_session, "inc_fbtest")
        db_session.commit()

        response = client.post("/api/incidents/inc_fbtest/nfa", json={"reason": "Noise alert"})
        assert response.status_code == 201
        data = response.json()
        assert data["nfa_count"] == 1
        assert data["status"] == "active"
        assert "pattern_id" in data

    def test_mark_nfa_auto_suppresses(self, client: TestClient, db_session: Session) -> None:
        db_session.commit()
        _create_incident(db_session, "inc_fbtest")
        db_session.commit()

        resp = None
        for _ in range(3):
            resp = client.post("/api/incidents/inc_fbtest/nfa", json={"reason": "Noise"})
            assert resp.status_code == 201

        assert resp is not None
        data = resp.json()
        assert data["status"] == "suppressed"

    def test_mark_nfa_requires_existing_incident(self, client: TestClient, db_session: Session) -> None:
        db_session.commit()
        response = client.post("/api/incidents/nonexistent/nfa", json={})
        assert response.status_code == 404


class TestRootCauseCorrectionAPI:
    def test_correct_root_cause_returns_feedback(self, client: TestClient, db_session: Session) -> None:
        db_session.commit()
        _create_incident(db_session, "inc_fbtest", root_cause="CPU saturation")
        db_session.commit()

        response = client.patch(
            "/api/incidents/inc_fbtest/root-cause",
            json={"corrected_summary": "Memory leak in checkout", "reason": "Wrong diagnosis"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["feedback_type"] == "root_cause_correction"
        assert data["delta"]["corrected"] == "Memory leak in checkout"

    def test_correct_root_cause_requires_summary(self, client: TestClient, db_session: Session) -> None:
        db_session.commit()
        _create_incident(db_session, "inc_fbtest")
        db_session.commit()

        response = client.patch("/api/incidents/inc_fbtest/root-cause", json={"corrected_summary": ""})
        assert response.status_code == 422


class TestActionCorrectionAPI:
    def test_correct_action_add(self, client: TestClient, db_session: Session) -> None:
        db_session.commit()
        _create_incident(db_session, "inc_fbtest")
        db_session.commit()

        response = client.post(
            "/api/incidents/inc_fbtest/actions/_/feedback",
            json={
                "action_type": "add",
                "action": {"type": "restart_pod", "target": "checkout-abc"},
                "reason": "Missing action",
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["feedback_type"] == "action_add"

    def test_correct_action_remove(self, client: TestClient, db_session: Session) -> None:
        db_session.commit()
        _create_incident(db_session, "inc_fbtest")
        db_session.commit()

        response = client.post(
            "/api/incidents/inc_fbtest/actions/act_1/feedback",
            json={
                "action_type": "remove",
                "action_id": "act_1",
                "reason": "Unsafe action",
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["feedback_type"] == "action_remove"


class TestCorrelatedIncidentsAPI:
    def test_get_correlated_returns_list(self, client: TestClient, db_session: Session) -> None:
        db_session.commit()
        _create_incident(db_session, "inc_fbtest", fingerprint="fp-x", service="checkout")
        _create_incident(db_session, "inc_related", fingerprint="fp-x", service="checkout",
                         alert_name="RelatedAlert", root_cause="Same fingerprint", status="resolved")
        db_session.commit()

        response = client.get("/api/incidents/inc_fbtest/correlated")
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        assert len(data) >= 1


class TestFeedbackListAPI:
    def test_list_feedback_returns_items(self, client: TestClient, db_session: Session) -> None:
        db_session.commit()
        _create_incident(db_session, "inc_fbtest", root_cause="Old")
        db_session.commit()

        client.patch(
            "/api/incidents/inc_fbtest/root-cause",
            json={"corrected_summary": "New diagnosis"},
        )

        response = client.get("/api/incidents/inc_fbtest/feedback")
        assert response.status_code == 200
        data = response.json()
        assert data["total"] >= 1
        assert len(data["items"]) >= 1
