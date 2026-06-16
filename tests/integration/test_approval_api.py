"""Integration tests for approval and action APIs."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from packages.common.ids import new_id
from packages.common.time import utc_now
from packages.db.models import Action, Approval, Incident


def _create_incident(db: Session, **kwargs) -> Incident:
    inc = Incident(
        incident_id=new_id("inc_"),
        fingerprint=kwargs.get("fingerprint", f"fp-{new_id('fp_')}"),
        source=kwargs.get("source", "mock"),
        service=kwargs.get("service", "checkout-api"),
        severity=kwargs.get("severity", "P1"),
        alert_name=kwargs.get("alert_name", "HighErrorRate"),
        status=kwargs.get("status", "waiting_approval"),
        starts_at=kwargs.get("starts_at", datetime(2026, 6, 1, 0, 0, tzinfo=UTC)),
        labels=kwargs.get("labels", {}),
        annotations=kwargs.get("annotations", {}),
    )
    db.add(inc)
    return inc


def _create_action(
    db: Session,
    incident_id: str,
    agent_run_id: str,
    type: str = "restart_pod",
    risk_level: str = "L2",
    status: str = "waiting_approval",
    target: str = "checkout-api",
    **kwargs,
) -> Action:
    action = Action(
        action_id=new_id("act_"),
        incident_id=incident_id,
        agent_run_id=agent_run_id,
        type=type,
        risk_level=risk_level,
        status=status,
        executor=kwargs.get("executor", "mock"),
        target=target,
        params=kwargs.get("params", {}),
        reason=kwargs.get("reason", "test action"),
        rollback_plan=kwargs.get("rollback_plan", ""),
    )
    db.add(action)
    return action


def _create_approval(
    db: Session,
    action_id: str,
    incident_id: str,
    agent_run_id: str,
    status: str = "waiting",
) -> Approval:
    approval = Approval(
        approval_id=new_id("apv_"),
        action_id=action_id,
        incident_id=incident_id,
        agent_run_id=agent_run_id,
        status=status,
        requested_at=utc_now(),
    )
    db.add(approval)
    return approval


class TestApprovalListAPI:
    def test_list_empty(self, client: TestClient, db_session: Session) -> None:
        resp = client.get("/api/approvals")
        assert resp.status_code == 200
        assert resp.json()["items"] == []
        assert resp.json()["total"] == 0

    def test_list_with_data(self, client: TestClient, db_session: Session) -> None:
        inc = _create_incident(db_session)
        run_id = new_id("run_")
        action = _create_action(db_session, inc.incident_id, run_id)
        approval = _create_approval(db_session, action.action_id, inc.incident_id, run_id)
        db_session.commit()

        resp = client.get("/api/approvals")
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert len(items) == 1
        assert items[0]["approval_id"] == approval.approval_id

    def test_filter_by_status(self, client: TestClient, db_session: Session) -> None:
        inc = _create_incident(db_session)
        run_id = new_id("run_")
        action = _create_action(db_session, inc.incident_id, run_id)
        _create_approval(db_session, action.action_id, inc.incident_id, run_id, status="waiting")
        db_session.commit()

        resp = client.get("/api/approvals?status=waiting")
        assert resp.status_code == 200
        assert len(resp.json()["items"]) == 1

        resp = client.get("/api/approvals?status=approved")
        assert resp.status_code == 200
        assert len(resp.json()["items"]) == 0

    def test_filter_by_risk_level(self, client: TestClient, db_session: Session) -> None:
        inc = _create_incident(db_session)
        run_id = new_id("run_")
        a1 = _create_action(
            db_session, inc.incident_id, run_id, type="restart_pod", risk_level="L2"
        )
        a2 = _create_action(
            db_session, inc.incident_id, run_id, type="rollback_release", risk_level="L3"
        )
        _create_approval(db_session, a1.action_id, inc.incident_id, run_id)
        _create_approval(db_session, a2.action_id, inc.incident_id, run_id)
        db_session.commit()

        resp = client.get("/api/approvals?risk_level=L3")
        assert resp.status_code == 200
        assert len(resp.json()["items"]) == 1

    def test_get_single_approval(self, client: TestClient, db_session: Session) -> None:
        inc = _create_incident(db_session)
        run_id = new_id("run_")
        action = _create_action(db_session, inc.incident_id, run_id)
        approval = _create_approval(db_session, action.action_id, inc.incident_id, run_id)
        db_session.commit()

        resp = client.get(f"/api/approvals/{approval.approval_id}")

        assert resp.status_code == 200
        body = resp.json()
        assert body["approval_id"] == approval.approval_id
        assert body["action_id"] == action.action_id
        assert body["service"] == inc.service

    def test_get_single_approval_not_found(self, client: TestClient) -> None:
        resp = client.get("/api/approvals/apv_missing")

        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "NOT_FOUND"

    def test_incident_approvals(self, client: TestClient, db_session: Session) -> None:
        inc = _create_incident(db_session)
        run_id = new_id("run_")
        action = _create_action(db_session, inc.incident_id, run_id)
        _create_approval(db_session, action.action_id, inc.incident_id, run_id)
        db_session.commit()

        resp = client.get(f"/api/incidents/{inc.incident_id}/approvals")
        assert resp.status_code == 200
        assert len(resp.json()) == 1


class TestApprovalApproveAPI:
    def test_approve_l2_success(self, client: TestClient, db_session: Session) -> None:
        inc = _create_incident(db_session)
        run_id = new_id("run_")
        action = _create_action(
            db_session, inc.incident_id, run_id, type="restart_pod", risk_level="L2"
        )
        approval = _create_approval(db_session, action.action_id, inc.incident_id, run_id)
        db_session.commit()

        resp = client.post(
            f"/api/approvals/{approval.approval_id}/approve",
            json={"approver": "sre-oncall", "comment": "approved"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "approved"

        db_session.refresh(approval)
        assert approval.status == "approved"
        db_session.refresh(action)
        assert action.status == "approved"

    def test_approve_l3_missing_risk_ack(self, client: TestClient, db_session: Session) -> None:
        inc = _create_incident(db_session)
        run_id = new_id("run_")
        action = _create_action(
            db_session, inc.incident_id, run_id, type="rollback_release", risk_level="L3"
        )
        approval = _create_approval(db_session, action.action_id, inc.incident_id, run_id)
        db_session.commit()

        resp = client.post(
            f"/api/approvals/{approval.approval_id}/approve",
            json={"approver": "sre-oncall", "comment": "approved"},
        )
        assert resp.status_code == 400

    def test_approve_l3_confirmation_valid(self, client: TestClient, db_session: Session) -> None:
        inc = _create_incident(db_session)
        run_id = new_id("run_")
        action = _create_action(
            db_session,
            inc.incident_id,
            run_id,
            type="rollback_release",
            risk_level="L3",
            target="checkout-api",
        )
        approval = _create_approval(db_session, action.action_id, inc.incident_id, run_id)
        db_session.commit()

        resp = client.post(
            f"/api/approvals/{approval.approval_id}/approve",
            json={
                "approver": "sre-oncall",
                "comment": "approved with confirmation",
                "risk_ack": True,
                "confirm_action_type": "rollback_release",
                "confirm_target": "checkout-api",
            },
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "approved"

        db_session.refresh(approval)
        assert approval.risk_ack is True
        assert approval.confirm_action_type == "rollback_release"
        assert approval.confirm_target == "checkout-api"

    def test_approve_l3_type_mismatch(self, client: TestClient, db_session: Session) -> None:
        inc = _create_incident(db_session)
        run_id = new_id("run_")
        action = _create_action(
            db_session, inc.incident_id, run_id, type="rollback_release", risk_level="L3"
        )
        approval = _create_approval(db_session, action.action_id, inc.incident_id, run_id)
        db_session.commit()

        resp = client.post(
            f"/api/approvals/{approval.approval_id}/approve",
            json={
                "approver": "sre-oncall",
                "risk_ack": True,
                "confirm_action_type": "wrong_type",
                "confirm_target": "checkout-api",
            },
        )
        assert resp.status_code == 400

    def test_approve_l3_target_mismatch(self, client: TestClient, db_session: Session) -> None:
        inc = _create_incident(db_session)
        run_id = new_id("run_")
        action = _create_action(
            db_session,
            inc.incident_id,
            run_id,
            type="rollback_release",
            risk_level="L3",
            target="checkout-api",
        )
        approval = _create_approval(db_session, action.action_id, inc.incident_id, run_id)
        db_session.commit()

        resp = client.post(
            f"/api/approvals/{approval.approval_id}/approve",
            json={
                "approver": "sre-oncall",
                "risk_ack": True,
                "confirm_action_type": "rollback_release",
                "confirm_target": "wrong-target",
            },
        )
        assert resp.status_code == 400

    def test_approve_already_decided(self, client: TestClient, db_session: Session) -> None:
        inc = _create_incident(db_session)
        run_id = new_id("run_")
        action = _create_action(db_session, inc.incident_id, run_id)
        approval = _create_approval(
            db_session, action.action_id, inc.incident_id, run_id, status="approved"
        )
        db_session.commit()

        resp = client.post(
            f"/api/approvals/{approval.approval_id}/approve",
            json={"approver": "sre-oncall", "comment": "retry"},
        )
        assert resp.status_code == 409


class TestBatchApprovalResume:
    """A run with multiple approvals must only resume once all are decided."""

    def _two_approval_run(self, db_session: Session) -> tuple[str, list[str]]:
        inc = _create_incident(db_session)
        run_id = new_id("run_")
        a1 = _create_action(
            db_session, inc.incident_id, run_id, type="scale_deployment", risk_level="L2"
        )
        a2 = _create_action(
            db_session, inc.incident_id, run_id, type="restart_pod", risk_level="L2"
        )
        p1 = _create_approval(db_session, a1.action_id, inc.incident_id, run_id)
        p2 = _create_approval(db_session, a2.action_id, inc.incident_id, run_id)
        db_session.commit()
        return run_id, [p1.approval_id, p2.approval_id]

    def test_first_approval_does_not_resume(
        self, client: TestClient, db_session: Session, fake_resume_enqueue
    ) -> None:
        _, [p1, p2] = self._two_approval_run(db_session)

        resp = client.post(f"/api/approvals/{p1}/approve", json={"approver": "sre"})
        assert resp.status_code == 200
        # Sibling p2 is still waiting → no resume yet (no stranding).
        assert fake_resume_enqueue.calls == []

        resp = client.post(f"/api/approvals/{p2}/approve", json={"approver": "sre"})
        assert resp.status_code == 200
        # Whole batch decided → exactly one resume.
        assert len(fake_resume_enqueue.calls) == 1

    def test_resume_fires_when_last_is_rejected(
        self, client: TestClient, db_session: Session, fake_resume_enqueue
    ) -> None:
        _, [p1, p2] = self._two_approval_run(db_session)

        client.post(f"/api/approvals/{p1}/approve", json={"approver": "sre"})
        assert fake_resume_enqueue.calls == []

        client.post(f"/api/approvals/{p2}/reject", json={"approver": "sre"})
        assert len(fake_resume_enqueue.calls) == 1

    def test_single_approval_resumes_immediately(
        self, client: TestClient, db_session: Session, fake_resume_enqueue
    ) -> None:
        inc = _create_incident(db_session)
        run_id = new_id("run_")
        action = _create_action(db_session, inc.incident_id, run_id, risk_level="L2")
        approval = _create_approval(db_session, action.action_id, inc.incident_id, run_id)
        db_session.commit()

        client.post(f"/api/approvals/{approval.approval_id}/approve", json={"approver": "sre"})
        assert len(fake_resume_enqueue.calls) == 1


class TestApprovalRejectAPI:
    def test_reject_success(self, client: TestClient, db_session: Session) -> None:
        inc = _create_incident(db_session)
        run_id = new_id("run_")
        action = _create_action(db_session, inc.incident_id, run_id)
        approval = _create_approval(db_session, action.action_id, inc.incident_id, run_id)
        db_session.commit()

        resp = client.post(
            f"/api/approvals/{approval.approval_id}/reject",
            json={"approver": "sre-oncall", "comment": "too risky"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "rejected"

        db_session.refresh(approval)
        assert approval.status == "rejected"
        db_session.refresh(action)
        assert action.status == "rejected"


class TestActionAPI:
    def test_get_action_detail(self, client: TestClient, db_session: Session) -> None:
        inc = _create_incident(db_session)
        run_id = new_id("run_")
        action = _create_action(
            db_session,
            inc.incident_id,
            run_id,
            type="restart_pod",
            risk_level="L2",
            reason="pod crashlooping",
            rollback_plan="scale back down",
        )
        db_session.commit()

        resp = client.get(f"/api/actions/{action.action_id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["action_id"] == action.action_id
        assert data["type"] == "restart_pod"
        assert data["risk_level"] == "L2"
        assert data["reason"] == "pod crashlooping"

    def test_get_action_not_found(self, client: TestClient) -> None:
        resp = client.get("/api/actions/act_nonexistent")
        assert resp.status_code == 404

    def test_execute_l4_blocked(self, client: TestClient, db_session: Session) -> None:
        inc = _create_incident(db_session)
        run_id = new_id("run_")
        action = _create_action(
            db_session,
            inc.incident_id,
            run_id,
            type="delete_data",
            risk_level="L4",
            status="proposed",
        )
        db_session.commit()

        resp = client.post(
            f"/api/actions/{action.action_id}/execute",
            json={"operator": "sre-oncall", "reason": "test"},
        )
        assert resp.status_code == 403
        db_session.refresh(action)
        assert action.status == "blocked"

    def test_execute_l2_without_approval(self, client: TestClient, db_session: Session) -> None:
        inc = _create_incident(db_session)
        run_id = new_id("run_")
        action = _create_action(
            db_session,
            inc.incident_id,
            run_id,
            type="restart_pod",
            risk_level="L2",
            status="proposed",
        )
        db_session.commit()

        resp = client.post(
            f"/api/actions/{action.action_id}/execute",
            json={"operator": "sre-oncall", "reason": "test"},
        )
        assert resp.status_code == 403

    def test_execute_l2_with_approval(self, client: TestClient, db_session: Session) -> None:
        inc = _create_incident(db_session)
        run_id = new_id("run_")
        action = _create_action(
            db_session,
            inc.incident_id,
            run_id,
            type="restart_pod",
            risk_level="L2",
            status="approved",
        )
        _create_approval(db_session, action.action_id, inc.incident_id, run_id, status="approved")
        db_session.commit()

        resp = client.post(
            f"/api/actions/{action.action_id}/execute",
            json={"operator": "sre-oncall", "reason": "retry"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "succeeded"
        db_session.refresh(action)
        assert action.status == "succeeded"
        assert action.execution_result is not None

    def test_execute_l3_with_full_approval(self, client: TestClient, db_session: Session) -> None:
        inc = _create_incident(db_session)
        run_id = new_id("run_")
        action = _create_action(
            db_session,
            inc.incident_id,
            run_id,
            type="rollback_release",
            risk_level="L3",
            status="approved",
            target="checkout-api",
        )
        approval = _create_approval(
            db_session, action.action_id, inc.incident_id, run_id, status="approved"
        )
        approval.risk_ack = True
        approval.confirm_action_type = "rollback_release"
        approval.confirm_target = "checkout-api"
        db_session.commit()

        resp = client.post(
            f"/api/actions/{action.action_id}/execute",
            json={"operator": "sre-oncall", "reason": "retry"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "succeeded"

    def test_execute_l3_rechecks_confirmation_matches_action(
        self, client: TestClient, db_session: Session
    ) -> None:
        inc = _create_incident(db_session)
        run_id = new_id("run_")
        action = _create_action(
            db_session,
            inc.incident_id,
            run_id,
            type="rollback_release",
            risk_level="L3",
            status="approved",
            target="checkout-api",
        )
        approval = _create_approval(
            db_session, action.action_id, inc.incident_id, run_id, status="approved"
        )
        approval.risk_ack = True
        approval.confirm_action_type = "rollback_release"
        approval.confirm_target = "payments-api"
        db_session.commit()

        resp = client.post(
            f"/api/actions/{action.action_id}/execute",
            json={"operator": "sre-oncall", "reason": "retry"},
        )

        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "APPROVAL_REQUIRED"
        db_session.refresh(action)
        assert action.status == "approved"
        assert action.execution_result is None

    def test_execute_l0_l1_auto(self, client: TestClient, db_session: Session) -> None:
        inc = _create_incident(db_session)
        run_id = new_id("run_")
        action = _create_action(
            db_session,
            inc.incident_id,
            run_id,
            type="create_ticket",
            risk_level="L1",
            status="proposed",
        )
        db_session.commit()

        resp = client.post(
            f"/api/actions/{action.action_id}/execute",
            json={"operator": "sre-oncall", "reason": "test"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "succeeded"

    def test_execute_already_executed(self, client: TestClient, db_session: Session) -> None:
        inc = _create_incident(db_session)
        run_id = new_id("run_")
        action = _create_action(
            db_session,
            inc.incident_id,
            run_id,
            type="create_ticket",
            risk_level="L1",
            status="succeeded",
        )
        db_session.commit()

        resp = client.post(
            f"/api/actions/{action.action_id}/execute",
            json={"operator": "sre-oncall", "reason": "retry"},
        )
        assert resp.status_code == 400
