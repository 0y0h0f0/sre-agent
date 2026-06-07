from __future__ import annotations

from datetime import UTC, datetime
from email.message import EmailMessage

from sqlalchemy import select
from sqlalchemy.orm import Session

from apps.api.services.email_service import (
    EmailContent,
    EmailNotificationService,
    EmailSendError,
    EmailService,
    parse_recipients,
)
from packages.common.ids import new_id
from packages.common.settings import Settings
from packages.db.models import Action, Approval, EmailLog, Incident


def _incident(db: Session) -> Incident:
    incident = Incident(
        incident_id=new_id("inc_"),
        fingerprint="fp-email",
        source="mock",
        service="checkout-api",
        severity="P1",
        alert_name="High5xxAfterDeploy",
        status="waiting_approval",
        starts_at=datetime(2026, 6, 1, 0, 0, tzinfo=UTC),
        labels={},
        annotations={"summary": "5xx spike"},
        raw_payload={"fingerprint": "fp-email"},
        root_cause_summary="Bad release caused elevated 5xx",
    )
    db.add(incident)
    return incident


def _action_and_approval(db: Session, incident: Incident) -> Approval:
    run_id = new_id("run_")
    action = Action(
        action_id=new_id("act_"),
        incident_id=incident.incident_id,
        agent_run_id=run_id,
        type="rollback_release",
        risk_level="L3",
        status="waiting_approval",
        executor="mock",
        target="checkout-api",
        params={},
        reason="new release correlated with 5xx spike",
        rollback_plan="redeploy previous stable version",
    )
    approval = Approval(
        approval_id=new_id("apv_"),
        action_id=action.action_id,
        incident_id=incident.incident_id,
        agent_run_id=run_id,
        status="waiting",
        requested_at=datetime(2026, 6, 1, 0, 4, tzinfo=UTC),
    )
    db.add_all([action, approval])
    return approval


def test_parse_recipients_accepts_commas_and_semicolons() -> None:
    assert parse_recipients("sre@example.com; oncall@example.com, lead@example.com") == [
        "sre@example.com",
        "oncall@example.com",
        "lead@example.com",
    ]


def test_l3_approval_email_contains_direct_link_and_confirmation_subject(db_session) -> None:
    incident = _incident(db_session)
    approval = _action_and_approval(db_session, incident)
    db_session.commit()
    settings = Settings(
        database_url="sqlite+pysqlite:///:memory:",
        sre_email_list="sre@example.com",
        web_base_url="http://console.local",
    )

    content = EmailNotificationService(db_session, settings).compose(
        "approval_request", {"approval_id": approval.approval_id}
    )

    assert content.subject == "[CONFIRM] L3 Action: rollback_release on checkout-api"
    assert content.recipients == ["sre@example.com"]
    assert f"http://console.local/approvals/{approval.approval_id}" in content.text_body
    assert "L3 actions require web UI confirmation" in content.html_body
    assert "cannot be approved via email link" in content.html_body


def test_email_send_event_logs_skipped_when_smtp_is_not_configured(db_session) -> None:
    incident = _incident(db_session)
    db_session.commit()
    settings = Settings(
        database_url="sqlite+pysqlite:///:memory:",
        sre_email_list="sre@example.com",
        smtp_host="",
    )

    result = EmailNotificationService(db_session, settings).send_event(
        "new_incident", {"incident_id": incident.incident_id}
    )

    assert result["status"] == "skipped"
    log = db_session.scalar(select(EmailLog).where(EmailLog.email_log_id == result["email_log_id"]))
    assert log is not None
    assert log.status == "skipped"
    assert log.related_incident_id == incident.incident_id
    assert log.last_error == "SMTP_HOST is not configured"


def test_retryable_send_failures_update_the_same_email_log(db_session, monkeypatch) -> None:
    incident = _incident(db_session)
    db_session.commit()
    settings = Settings(
        database_url="sqlite+pysqlite:///:memory:",
        sre_email_list="sre@example.com",
        smtp_host="smtp.local",
    )
    service = EmailNotificationService(db_session, settings)

    def _fail(_content):
        raise EmailSendError("temporary SMTP outage", retryable=True)

    monkeypatch.setattr(service.email, "send_sync", _fail)
    queued = service.queue_event("new_incident", {"incident_id": incident.incident_id})

    first = service.send_queued_event(
        queued["email_log_id"],
        "new_incident",
        {"incident_id": incident.incident_id},
        attempt=1,
    )
    second = service.send_queued_event(
        queued["email_log_id"],
        "new_incident",
        {"incident_id": incident.incident_id},
        attempt=2,
    )

    logs = list(db_session.scalars(select(EmailLog)))
    assert first["status"] == "failed"
    assert second["status"] == "failed"
    assert len(logs) == 1
    assert logs[0].email_log_id == queued["email_log_id"]
    assert logs[0].attempts == 2
    assert logs[0].last_error == "temporary SMTP outage"


def test_send_event_skipped_uses_one_email_log(db_session) -> None:
    incident = _incident(db_session)
    db_session.commit()
    settings = Settings(
        database_url="sqlite+pysqlite:///:memory:",
        sre_email_list="",
        smtp_host="smtp.local",
    )

    result = EmailNotificationService(db_session, settings).send_event(
        "new_incident", {"incident_id": incident.incident_id}
    )

    logs = list(db_session.scalars(select(EmailLog)))
    assert result["status"] == "skipped"
    assert len(logs) == 1
    assert logs[0].email_log_id == result["email_log_id"]
    assert logs[0].recipient_count == 0
    assert logs[0].last_error == "no recipients configured"


def test_email_service_uses_implicit_tls_for_port_465(monkeypatch) -> None:
    import aiosmtplib

    captured: dict[str, object] = {}

    async def _send(message: EmailMessage, **kwargs: object) -> str:
        captured.update(kwargs)
        assert message["Subject"] == "SMTP TLS smoke"
        return "provider-message-id"

    monkeypatch.setattr(aiosmtplib, "send", _send)
    settings = Settings(
        database_url="sqlite+pysqlite:///:memory:",
        smtp_host="smtp.example.com",
        smtp_port=465,
        smtp_tls_mode="auto",
        smtp_from="agent@example.com",
    )

    result = EmailService(settings).send_sync(
        EmailContent(
            notification_type="smtp_tls_smoke",
            subject="SMTP TLS smoke",
            recipients=["sre@example.com"],
            html_body="<p>ok</p>",
            text_body="ok",
        )
    )

    assert result == "provider-message-id"
    assert captured["use_tls"] is True
    assert captured["start_tls"] is False


def test_email_service_rejects_invalid_tls_mode() -> None:
    settings = Settings(
        database_url="sqlite+pysqlite:///:memory:",
        smtp_host="smtp.example.com",
        smtp_tls_mode="bad-mode",
        smtp_from="agent@example.com",
    )

    try:
        EmailService(settings).send_sync(
            EmailContent(
                notification_type="smtp_tls_smoke",
                subject="SMTP TLS smoke",
                recipients=["sre@example.com"],
                html_body="<p>ok</p>",
                text_body="ok",
            )
        )
    except EmailSendError as exc:
        assert exc.retryable is False
        assert "SMTP_TLS_MODE" in str(exc)
    else:  # pragma: no cover - defensive assertion
        raise AssertionError("invalid SMTP_TLS_MODE should fail before sending")
