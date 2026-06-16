"""Integration tests for Phase 6 collaboration & approval enhancement features."""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from packages.common.ids import new_id
from packages.db.models import (
    Action,
    Approval,
    EvidenceItem,
    Incident,
)


def _create_incident(db: Session, **kwargs) -> Incident:
    from datetime import UTC, datetime

    inc = Incident(
        incident_id=new_id("inc_"),
        fingerprint=kwargs.get("fingerprint", f"fp-{new_id('fp_')}"),
        source=kwargs.get("source", "mock"),
        service=kwargs.get("service", "checkout-api"),
        severity=kwargs.get("severity", "P1"),
        alert_name=kwargs.get("alert_name", "HighErrorRate"),
        status=kwargs.get("status", "open"),
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
        target=kwargs.get("target", "checkout-api"),
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
    from packages.common.time import utc_now

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


def _create_evidence(db: Session, incident_id: str, agent_run_id: str) -> EvidenceItem:
    ev = EvidenceItem(
        evidence_id=new_id("evd_"),
        incident_id=incident_id,
        agent_run_id=agent_run_id,
        type="metrics",
        source="prometheus",
        title="High latency spike",
        excerpt="P99 latency jumped to 5s",
    )
    db.add(ev)
    return ev


class TestCommentAPI:
    """Phase 6.1: Multi-person comments."""

    def test_create_and_list_comments(self, client: TestClient, db_session: Session) -> None:
        inc = _create_incident(db_session)
        db_session.commit()

        resp = client.post(
            f"/api/incidents/{inc.incident_id}/comments",
            json={"author": "alice", "content": "Looking into this"},
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["author"] == "alice"
        assert data["content"] == "Looking into this"
        assert data["mentioned_users"] == []

        resp = client.get(f"/api/incidents/{inc.incident_id}/comments")
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert len(items) == 1
        assert items[0]["author"] == "alice"

    def test_comment_with_mentions(self, client: TestClient, db_session: Session) -> None:
        inc = _create_incident(db_session)
        db_session.commit()

        resp = client.post(
            f"/api/incidents/{inc.incident_id}/comments",
            json={
                "author": "bob",
                "content": "@charlie can you check the metrics?",
                "mentioned_users": ["charlie"],
            },
        )
        assert resp.status_code == 201
        assert resp.json()["mentioned_users"] == ["charlie"]

    def test_threaded_comments(self, client: TestClient, db_session: Session) -> None:
        inc = _create_incident(db_session)
        db_session.commit()

        parent = client.post(
            f"/api/incidents/{inc.incident_id}/comments",
            json={"author": "sre", "content": "Root cause?"},
        )
        assert parent.status_code == 201
        parent_id = parent.json()["comment_id"]

        reply = client.post(
            f"/api/incidents/{inc.incident_id}/comments",
            json={
                "author": "alice",
                "content": "Memory leak in checkout-api",
                "parent_comment_id": parent_id,
            },
        )
        assert reply.status_code == 201
        assert reply.json()["parent_comment_id"] == parent_id

    def test_delete_comment(self, client: TestClient, db_session: Session) -> None:
        inc = _create_incident(db_session)
        db_session.commit()

        created = client.post(
            f"/api/incidents/{inc.incident_id}/comments",
            json={"author": "sre", "content": "delete me"},
        )
        comment_id = created.json()["comment_id"]

        resp = client.delete(f"/api/comments/{comment_id}")
        assert resp.status_code == 204

        resp = client.get(f"/api/incidents/{inc.incident_id}/comments")
        assert resp.json()["total"] == 0

    def test_comment_incident_not_found(self, client: TestClient) -> None:
        resp = client.post(
            "/api/incidents/inc_missing/comments",
            json={"author": "sre", "content": "test"},
        )
        assert resp.status_code == 404


class TestEvidenceAnnotationAPI:
    """Phase 6.1: Evidence annotations."""

    def test_create_and_list_annotations(self, client: TestClient, db_session: Session) -> None:
        inc = _create_incident(db_session)
        run_id = new_id("run_")
        ev = _create_evidence(db_session, inc.incident_id, run_id)
        db_session.commit()

        resp = client.post(
            f"/api/evidence/{ev.evidence_id}/annotations",
            json={"author": "alice", "content": "This spike correlates with the deploy"},
        )
        assert resp.status_code == 201
        assert resp.json()["author"] == "alice"

        resp = client.get(f"/api/evidence/{ev.evidence_id}/annotations")
        assert resp.status_code == 200
        assert len(resp.json()["items"]) == 1

    def test_annotation_evidence_not_found(self, client: TestClient) -> None:
        resp = client.post(
            "/api/evidence/evd_missing/annotations",
            json={"author": "sre", "content": "test"},
        )
        assert resp.status_code == 404


class TestAuditLogAPI:
    """Phase 6.1: Operation audit trail."""

    def test_audit_logged_on_approval(self, client: TestClient, db_session: Session) -> None:
        inc = _create_incident(db_session, status="waiting_approval")
        run_id = new_id("run_")
        action = _create_action(db_session, inc.incident_id, run_id, risk_level="L2")
        approval = _create_approval(db_session, action.action_id, inc.incident_id, run_id)
        db_session.commit()

        client.post(
            f"/api/approvals/{approval.approval_id}/approve",
            json={"approver": "sre-oncall"},
        )

        resp = client.get(f"/api/incidents/{inc.incident_id}/audit")
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert len(items) == 1
        assert items[0]["action"] == "approve"
        assert items[0]["actor"] == "sre-oncall"

    def test_audit_logged_on_nfa(self, client: TestClient, db_session: Session) -> None:
        inc = _create_incident(db_session)
        db_session.commit()

        client.post(
            f"/api/incidents/{inc.incident_id}/nfa",
            json={"reason": "false alarm"},
        )

        resp = client.get(f"/api/incidents/{inc.incident_id}/audit")
        assert resp.status_code == 200
        actions = [i["action"] for i in resp.json()["items"]]
        assert "nfa_mark" in actions

    def test_audit_logged_on_root_cause_correction(
        self, client: TestClient, db_session: Session
    ) -> None:
        inc = _create_incident(db_session)
        db_session.commit()

        client.patch(
            f"/api/incidents/{inc.incident_id}/root-cause",
            json={"corrected_summary": "Actual root cause"},
        )

        resp = client.get(f"/api/incidents/{inc.incident_id}/audit")
        assert resp.status_code == 200
        actions = [i["action"] for i in resp.json()["items"]]
        assert "root_cause_correct" in actions

    def test_audit_empty(self, client: TestClient, db_session: Session) -> None:
        inc = _create_incident(db_session)
        db_session.commit()

        resp = client.get(f"/api/incidents/{inc.incident_id}/audit")
        assert resp.status_code == 200
        assert resp.json()["items"] == []

    def test_audit_comment_creates_entry(self, client: TestClient, db_session: Session) -> None:
        inc = _create_incident(db_session)
        db_session.commit()

        client.post(
            f"/api/incidents/{inc.incident_id}/comments",
            json={"author": "alice", "content": "Found the issue"},
        )

        resp = client.get(f"/api/incidents/{inc.incident_id}/audit")
        assert resp.status_code == 200
        actions = [i["action"] for i in resp.json()["items"]]
        assert "comment_add" in actions


class TestBatchApprovalAPI:
    """Phase 6.2: Batch approval."""

    def test_batch_approve(self, client: TestClient, db_session: Session) -> None:
        inc = _create_incident(db_session, status="waiting_approval")
        run_id = new_id("run_")
        a1 = _create_action(
            db_session, inc.incident_id, run_id,
            type="restart_pod", risk_level="L2",
        )
        a2 = _create_action(db_session, inc.incident_id, run_id, type="scale_up", risk_level="L2")
        p1 = _create_approval(db_session, a1.action_id, inc.incident_id, run_id)
        p2 = _create_approval(db_session, a2.action_id, inc.incident_id, run_id)
        db_session.commit()

        resp = client.post(
            "/api/approvals/batch",
            json={
                "decision": "approve",
                "approver": "sre-batch",
                "approval_ids": [p1.approval_id, p2.approval_id],
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        assert all(r["status"] == "approved" for r in data)

    def test_batch_reject(self, client: TestClient, db_session: Session) -> None:
        inc = _create_incident(db_session, status="waiting_approval")
        run_id = new_id("run_")
        action = _create_action(db_session, inc.incident_id, run_id, risk_level="L2")
        approval = _create_approval(db_session, action.action_id, inc.incident_id, run_id)
        db_session.commit()

        resp = client.post(
            "/api/approvals/batch",
            json={
                "decision": "reject",
                "approver": "sre-batch",
                "approval_ids": [approval.approval_id],
            },
        )
        assert resp.status_code == 200
        assert resp.json()[0]["status"] == "rejected"

    def test_batch_approve_is_atomic_when_l3_confirmation_missing(
        self,
        client: TestClient,
        db_session: Session,
    ) -> None:
        inc = _create_incident(db_session, status="waiting_approval")
        run_id = new_id("run_")
        l2_action = _create_action(
            db_session,
            inc.incident_id,
            run_id,
            type="restart_pod",
            risk_level="L2",
        )
        l3_action = _create_action(
            db_session,
            inc.incident_id,
            run_id,
            type="rollback_release",
            risk_level="L3",
            target="checkout-api",
        )
        l2_approval = _create_approval(
            db_session,
            l2_action.action_id,
            inc.incident_id,
            run_id,
        )
        l3_approval = _create_approval(
            db_session,
            l3_action.action_id,
            inc.incident_id,
            run_id,
        )
        db_session.commit()

        resp = client.post(
            "/api/approvals/batch",
            json={
                "decision": "approve",
                "approver": "sre-batch",
                "approval_ids": [l2_approval.approval_id, l3_approval.approval_id],
            },
        )

        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "VALIDATION_ERROR"
        db_session.refresh(l2_action)
        db_session.refresh(l3_action)
        db_session.refresh(l2_approval)
        db_session.refresh(l3_approval)
        assert l2_action.status == "waiting_approval"
        assert l3_action.status == "waiting_approval"
        assert l2_approval.status == "waiting"
        assert l3_approval.status == "waiting"

    def test_batch_empty_ids(self, client: TestClient) -> None:
        resp = client.post(
            "/api/approvals/batch",
            json={"decision": "approve", "approver": "sre", "approval_ids": []},
        )
        assert resp.status_code == 422


class TestEmailTokenApprovalAPI:
    """Phase 6.2: Email token-based approval."""

    def test_generate_and_use_token_approve(self, client: TestClient, db_session: Session) -> None:
        inc = _create_incident(db_session, status="waiting_approval")
        run_id = new_id("run_")
        action = _create_action(db_session, inc.incident_id, run_id, risk_level="L2")
        approval = _create_approval(db_session, action.action_id, inc.incident_id, run_id)
        db_session.commit()

        # Generate token
        resp = client.post(f"/api/approvals/{approval.approval_id}/email-token")
        assert resp.status_code == 200
        token = resp.json()["email_token"]
        assert len(token) > 0

        # Approve by token
        resp = client.post(
            f"/api/approvals/by-token/{token}/approve",
            json={"approver": "email-user"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "approved"

        # Token consumed — reuse should fail
        resp = client.post(
            f"/api/approvals/by-token/{token}/approve",
            json={"approver": "email-user"},
        )
        assert resp.status_code == 404

    def test_token_reject(self, client: TestClient, db_session: Session) -> None:
        inc = _create_incident(db_session, status="waiting_approval")
        run_id = new_id("run_")
        action = _create_action(db_session, inc.incident_id, run_id, risk_level="L2")
        approval = _create_approval(db_session, action.action_id, inc.incident_id, run_id)
        db_session.commit()

        resp = client.post(f"/api/approvals/{approval.approval_id}/email-token")
        token = resp.json()["email_token"]

        resp = client.post(
            f"/api/approvals/by-token/{token}/reject",
            json={"approver": "email-user"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "rejected"

    def test_token_l3_blocked(self, client: TestClient, db_session: Session) -> None:
        """L3 actions cannot be approved via email token."""
        inc = _create_incident(db_session, status="waiting_approval")
        run_id = new_id("run_")
        action = _create_action(
            db_session, inc.incident_id, run_id, type="rollback_release", risk_level="L3"
        )
        approval = _create_approval(db_session, action.action_id, inc.incident_id, run_id)
        db_session.commit()

        resp = client.post(f"/api/approvals/{approval.approval_id}/email-token")
        token = resp.json()["email_token"]

        resp = client.post(
            f"/api/approvals/by-token/{token}/approve",
            json={"approver": "email-user"},
        )
        assert resp.status_code == 400

    def test_invalid_token(self, client: TestClient) -> None:
        resp = client.post(
            "/api/approvals/by-token/bad_token/approve",
            json={"approver": "test"},
        )
        assert resp.status_code == 404

    # ------------------------------------------------------------------
    # Phase 9: GET confirmation page + POST redirect tests (R4.1, R4.2)
    # ------------------------------------------------------------------

    def test_get_confirm_page_renders_html(
        self, client: TestClient, db_session: Session
    ) -> None:
        """GET /approvals/by-token/{token}/approve renders a confirmation page."""
        inc = _create_incident(db_session, status="waiting_approval")
        run_id = new_id("run_")
        action = _create_action(db_session, inc.incident_id, run_id, risk_level="L2")
        approval = _create_approval(db_session, action.action_id, inc.incident_id, run_id)
        db_session.commit()
        resp = client.post(f"/api/approvals/{approval.approval_id}/email-token")
        token = resp.json()["email_token"]

        resp = client.get(f"/api/approvals/by-token/{token}/approve")
        assert resp.status_code == 200
        html = resp.text
        assert "Confirm Approve" in html
        assert action.type in html
        assert "L2" in html
        assert f'/api/approvals/by-token/{token}/approve' in html
        assert f'redirect=/incidents/{inc.incident_id}' in html

    def test_get_reject_page_renders_html(
        self, client: TestClient, db_session: Session
    ) -> None:
        """GET /approvals/by-token/{token}/reject renders a confirmation page."""
        inc = _create_incident(db_session, status="waiting_approval")
        run_id = new_id("run_")
        action = _create_action(db_session, inc.incident_id, run_id, risk_level="L2")
        approval = _create_approval(db_session, action.action_id, inc.incident_id, run_id)
        db_session.commit()
        resp = client.post(f"/api/approvals/{approval.approval_id}/email-token")
        token = resp.json()["email_token"]

        resp = client.get(f"/api/approvals/by-token/{token}/reject")
        assert resp.status_code == 200
        html = resp.text
        assert "Confirm Reject" in html

    def test_get_confirm_page_bad_token(
        self, client: TestClient
    ) -> None:
        """GET confirmation page with invalid token returns error HTML."""
        resp = client.get("/api/approvals/by-token/bad_token/approve")
        assert resp.status_code == 400
        assert "Unavailable" in resp.text

    def test_post_without_redirect_returns_json(
        self, client: TestClient, db_session: Session
    ) -> None:
        """POST without redirect returns JSON (existing behavior)."""
        inc = _create_incident(db_session, status="waiting_approval")
        run_id = new_id("run_")
        action = _create_action(db_session, inc.incident_id, run_id, risk_level="L2")
        approval = _create_approval(db_session, action.action_id, inc.incident_id, run_id)
        db_session.commit()
        resp = client.post(f"/api/approvals/{approval.approval_id}/email-token")
        token = resp.json()["email_token"]

        resp = client.post(
            f"/api/approvals/by-token/{token}/approve",
            json={"approver": "test-user"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "approved"

    def test_confirm_page_form_has_redirect_in_action(
        self, client: TestClient, db_session: Session
    ) -> None:
        """Confirmation page form action includes ?redirect=/incidents/{id}."""
        inc = _create_incident(db_session, status="waiting_approval")
        run_id = new_id("run_")
        action = _create_action(db_session, inc.incident_id, run_id, risk_level="L2")
        approval = _create_approval(db_session, action.action_id, inc.incident_id, run_id)
        db_session.commit()
        resp = client.post(f"/api/approvals/{approval.approval_id}/email-token")
        token = resp.json()["email_token"]

        resp = client.get(f"/api/approvals/by-token/{token}/approve")
        assert resp.status_code == 200
        html = resp.text
        # The form should POST to the approve URL with redirect to the incident
        expected = (
            f'action="/api/approvals/by-token/{token}/approve'
            f'?redirect=/incidents/{inc.incident_id}"'
        )
        assert expected in html

    def test_token_consumed_after_use(
        self, client: TestClient, db_session: Session
    ) -> None:
        """Token is consumed after first successful GET + POST. Second GET fails."""
        inc = _create_incident(db_session, status="waiting_approval")
        run_id = new_id("run_")
        action = _create_action(db_session, inc.incident_id, run_id, risk_level="L2")
        approval = _create_approval(db_session, action.action_id, inc.incident_id, run_id)
        db_session.commit()
        resp = client.post(f"/api/approvals/{approval.approval_id}/email-token")
        token = resp.json()["email_token"]

        # First GET works
        resp = client.get(f"/api/approvals/by-token/{token}/approve")
        assert resp.status_code == 200

        # POST to approve
        resp = client.post(
            f"/api/approvals/by-token/{token}/approve",
            json={"approver": "test-user"},
        )
        assert resp.status_code == 200

        # Token consumed — second GET shows error
        resp = client.get(f"/api/approvals/by-token/{token}/approve")
        assert resp.status_code == 400
        assert "Unavailable" in resp.text


class TestApprovalGroupAPI:
    """Phase 6.2: Approval group CRUD."""

    def test_create_and_list_groups(self, client: TestClient, db_session: Session) -> None:
        resp = client.post(
            "/api/approval-groups",
            json={
                "name": "Payments Team",
                "service_pattern": r"checkout-.*|payment-.*",
                "members": ["alice", "bob"],
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "Payments Team"
        assert data["members"] == ["alice", "bob"]

        resp = client.get("/api/approval-groups")
        assert resp.status_code == 200
        assert len(resp.json()["items"]) == 1

    def test_update_group(self, client: TestClient, db_session: Session) -> None:
        resp = client.post(
            "/api/approval-groups",
            json={"name": "Test Group", "service_pattern": "test-.*", "members": []},
        )
        group_id = resp.json()["group_id"]

        resp = client.patch(
            f"/api/approval-groups/{group_id}",
            json={"members": ["charlie"]},
        )
        assert resp.status_code == 200
        assert resp.json()["members"] == ["charlie"]

    def test_delete_group(self, client: TestClient, db_session: Session) -> None:
        resp = client.post(
            "/api/approval-groups",
            json={"name": "To Delete", "service_pattern": "tmp-.*", "members": []},
        )
        group_id = resp.json()["group_id"]

        resp = client.delete(f"/api/approval-groups/{group_id}")
        assert resp.status_code == 204

        resp = client.get(f"/api/approval-groups/{group_id}")
        assert resp.status_code == 404

    def test_duplicate_name_blocked(self, client: TestClient, db_session: Session) -> None:
        client.post(
            "/api/approval-groups",
            json={"name": "Unique", "service_pattern": "a-.*", "members": []},
        )
        resp = client.post(
            "/api/approval-groups",
            json={"name": "Unique", "service_pattern": "b-.*", "members": []},
        )
        assert resp.status_code == 409

    def test_group_not_found(self, client: TestClient) -> None:
        resp = client.get("/api/approval-groups/agp_missing")
        assert resp.status_code == 404
