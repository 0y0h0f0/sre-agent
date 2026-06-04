"""Repository for outbound email notification audit logs."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from packages.common.ids import new_id
from packages.common.time import utc_now
from packages.db.models import EmailLog


class EmailLogRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def create(
        self,
        *,
        notification_type: str,
        recipients: list[str],
        subject: str,
        related_incident_id: str | None = None,
        related_agent_run_id: str | None = None,
        related_approval_id: str | None = None,
        related_report_id: str | None = None,
    ) -> EmailLog:
        log = EmailLog(
            email_log_id=new_id("eml_"),
            notification_type=notification_type,
            status="queued",
            recipients=recipients,
            recipient_count=len(recipients),
            subject=subject[:255],
            related_incident_id=related_incident_id,
            related_agent_run_id=related_agent_run_id,
            related_approval_id=related_approval_id,
            related_report_id=related_report_id,
            attempts=0,
        )
        self.db.add(log)
        return log

    def get_by_public_id(self, email_log_id: str) -> EmailLog | None:
        stmt = select(EmailLog).where(EmailLog.email_log_id == email_log_id)
        return self.db.scalar(stmt)

    def mark_sent(
        self,
        log: EmailLog,
        *,
        attempts: int,
        provider_message_id: str | None = None,
    ) -> EmailLog:
        log.status = "sent"
        log.attempts = attempts
        log.provider_message_id = provider_message_id
        log.last_error = None
        log.sent_at = utc_now()
        return log

    def mark_failed(self, log: EmailLog, *, attempts: int, error: str) -> EmailLog:
        log.status = "failed"
        log.attempts = attempts
        log.last_error = error[:2000]
        return log

    def mark_skipped(self, log: EmailLog, *, reason: str) -> EmailLog:
        log.status = "skipped"
        log.attempts = 0
        log.last_error = reason[:2000]
        return log

    def mark_enqueue_failed(self, email_log_id: str, error: str) -> EmailLog | None:
        log = self.get_by_public_id(email_log_id)
        if log is None:
            return None
        log.status = "failed"
        log.attempts = 0
        log.last_error = f"notification enqueue failed: {error}"[:2000]
        return log

    def exists_for_event(
        self,
        *,
        notification_type: str,
        related_incident_id: str | None = None,
        related_agent_run_id: str | None = None,
        related_approval_id: str | None = None,
        related_report_id: str | None = None,
    ) -> bool:
        stmt = select(EmailLog).where(EmailLog.notification_type == notification_type)
        if related_incident_id is not None:
            stmt = stmt.where(EmailLog.related_incident_id == related_incident_id)
        if related_agent_run_id is not None:
            stmt = stmt.where(EmailLog.related_agent_run_id == related_agent_run_id)
        if related_approval_id is not None:
            stmt = stmt.where(EmailLog.related_approval_id == related_approval_id)
        if related_report_id is not None:
            stmt = stmt.where(EmailLog.related_report_id == related_report_id)
        return self.db.scalar(stmt.limit(1)) is not None

    def list_for_incident(self, incident_id: str) -> Sequence[EmailLog]:
        stmt = (
            select(EmailLog)
            .where(EmailLog.related_incident_id == incident_id)
            .order_by(EmailLog.created_at.desc(), EmailLog.id.desc())
        )
        return self.db.scalars(stmt).all()
