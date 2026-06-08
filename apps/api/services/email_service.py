"""SMTP email notification service for Phase 3 alert and incident workflows."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from email.message import EmailMessage
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy import select
from sqlalchemy.orm import Session

from packages.common.settings import Settings
from packages.common.time import utc_now
from packages.db.models import Incident
from packages.db.repositories.actions import ActionRepository
from packages.db.repositories.approvals import ApprovalRepository
from packages.db.repositories.email_logs import EmailLogRepository
from packages.db.repositories.incidents import IncidentRepository
from packages.db.repositories.incidents_read import IncidentReadRepository
from packages.db.repositories.reports import IncidentReportRepository


@dataclass(frozen=True)
class EmailContent:
    notification_type: str
    subject: str
    recipients: list[str]
    html_body: str
    text_body: str
    related_incident_id: str | None = None
    related_agent_run_id: str | None = None
    related_approval_id: str | None = None
    related_report_id: str | None = None


class EmailSendError(Exception):
    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


class EmailService:
    def __init__(self, settings: Settings, template_dir: Path | None = None) -> None:
        self.settings = settings
        default_dir = Path(__file__).resolve().parents[3] / "templates" / "email"
        self.template_dir = template_dir or default_dir
        self.env = Environment(
            loader=FileSystemLoader(str(self.template_dir)),
            autoescape=select_autoescape(["html", "xml"]),
        )

    async def send(self, content: EmailContent) -> str | None:
        if not content.recipients:
            raise EmailSendError("no recipients configured", retryable=False)
        if not self.settings.smtp_host:
            raise EmailSendError("SMTP_HOST is not configured", retryable=False)
        if not self.settings.smtp_from:
            raise EmailSendError("SMTP_FROM is not configured", retryable=False)

        try:
            import aiosmtplib
        except ImportError as exc:  # pragma: no cover - depends on local install state
            raise EmailSendError("aiosmtplib is not installed", retryable=False) from exc

        message = EmailMessage()
        message["From"] = self.settings.smtp_from
        message["To"] = ", ".join(content.recipients)
        message["Subject"] = content.subject
        message.set_content(content.text_body)
        message.add_alternative(content.html_body, subtype="html")

        username = self.settings.smtp_user or None
        password = (
            self.settings.smtp_password.get_secret_value()
            if self.settings.smtp_password is not None
            else None
        ) or None
        use_tls, start_tls = self._smtp_tls_flags()
        try:
            result = await aiosmtplib.send(
                message,
                hostname=self.settings.smtp_host,
                port=self.settings.smtp_port,
                username=username,
                password=password,
                use_tls=use_tls,
                start_tls=start_tls,
                timeout=self.settings.smtp_timeout_seconds,
            )
        except Exception as exc:  # pragma: no cover - SMTP servers vary
            raise EmailSendError(str(exc), retryable=True) from exc
        return str(result) if result is not None else None

    def send_sync(self, content: EmailContent) -> str | None:
        from time import perf_counter

        from packages.common import metrics as agent_metrics

        started = perf_counter()
        try:
            result = asyncio.run(self.send(content))
            agent_metrics.AgentMetricsCollector.record_email_send(
                notification_type=content.notification_type,
                status="sent",
                duration_seconds=perf_counter() - started,
            )
            return result
        except Exception:
            agent_metrics.AgentMetricsCollector.record_email_send(
                notification_type=content.notification_type,
                status="failed",
                duration_seconds=perf_counter() - started,
            )
            raise

    def _smtp_tls_flags(self) -> tuple[bool, bool]:
        mode = self.settings.smtp_tls_mode.strip().lower()
        if mode == "auto":
            return self.settings.smtp_port == 465, self.settings.smtp_port == 587
        if mode == "tls":
            return True, False
        if mode == "starttls":
            return False, True
        if mode == "none":
            return False, False
        raise EmailSendError(
            "SMTP_TLS_MODE must be one of: auto, starttls, tls, none", retryable=False
        )

    def render_html(self, template_name: str, context: dict[str, Any]) -> str:
        return self.env.get_template(template_name).render(**context)


class EmailNotificationService:
    def __init__(self, db: Session, settings: Settings) -> None:
        self.db = db
        self.settings = settings
        self.incidents = IncidentRepository(db)
        self.reads = IncidentReadRepository(db)
        self.approvals = ApprovalRepository(db)
        self.actions = ActionRepository(db)
        self.reports = IncidentReportRepository(db)
        self.email_logs = EmailLogRepository(db)
        self.email = EmailService(settings)

    def queue_event(self, notification_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        content = self.compose(notification_type, payload)
        log = self.email_logs.create(
            notification_type=content.notification_type,
            recipients=content.recipients,
            subject=content.subject,
            related_incident_id=content.related_incident_id,
            related_agent_run_id=content.related_agent_run_id,
            related_approval_id=content.related_approval_id,
            related_report_id=content.related_report_id,
        )
        self.db.commit()
        return {
            "email_log_id": log.email_log_id,
            "notification_type": content.notification_type,
            "status": log.status,
        }

    def mark_enqueue_failed(self, email_log_id: str, error: str) -> None:
        self.email_logs.mark_enqueue_failed(email_log_id, error)
        self.db.commit()

    def send_event(
        self,
        notification_type: str,
        payload: dict[str, Any],
        *,
        attempt: int = 1,
    ) -> dict[str, Any]:
        queued = self.queue_event(notification_type, payload)
        return self.send_queued_event(
            queued["email_log_id"], notification_type, payload, attempt=attempt
        )

    def send_queued_event(
        self,
        email_log_id: str,
        notification_type: str,
        payload: dict[str, Any],
        *,
        attempt: int = 1,
    ) -> dict[str, Any]:
        log = self.email_logs.get_by_public_id(email_log_id)
        if log is None:
            raise ValueError(f"email log not found: {email_log_id}")

        content = self.compose(notification_type, payload)
        log.recipients = content.recipients
        log.recipient_count = len(content.recipients)
        log.subject = content.subject[:255]
        log.related_incident_id = content.related_incident_id
        log.related_agent_run_id = content.related_agent_run_id
        log.related_approval_id = content.related_approval_id
        log.related_report_id = content.related_report_id
        try:
            provider_message_id = self.email.send_sync(content)
        except EmailSendError as exc:
            if exc.retryable:
                self.email_logs.mark_failed(log, attempts=attempt, error=str(exc))
                self.db.commit()
                return {
                    "email_log_id": log.email_log_id,
                    "status": "failed",
                    "retryable": True,
                    "error": str(exc),
                }
            self.email_logs.mark_skipped(log, reason=str(exc))
            self.db.commit()
            return {
                "email_log_id": log.email_log_id,
                "status": "skipped",
                "retryable": False,
                "error": str(exc),
            }

        self.email_logs.mark_sent(log, attempts=attempt, provider_message_id=provider_message_id)
        self.db.commit()
        return {"email_log_id": log.email_log_id, "status": "sent", "retryable": False}

    def compose(self, notification_type: str, payload: dict[str, Any]) -> EmailContent:
        if notification_type == "new_incident":
            return self._new_incident(payload)
        if notification_type == "diagnosis_complete":
            return self._diagnosis_complete(payload)
        if notification_type == "approval_request":
            return self._approval_request(payload)
        if notification_type == "incident_report":
            return self._incident_report(payload)
        if notification_type == "daily_summary":
            return self._daily_summary(payload)
        raise ValueError(f"unknown email notification type: {notification_type}")

    def _new_incident(self, payload: dict[str, Any]) -> EmailContent:
        incident = self._require_incident(str(payload.get("incident_id", "")))
        context = {
            "incident": incident,
            "incident_url": self._url(f"/incidents/{incident.incident_id}"),
        }
        subject = f"[{incident.severity}] New Incident: {incident.alert_name}"
        html = self.email.render_html("incident_alert.html", context)
        text = _lines(
            subject,
            f"Service: {incident.service}",
            f"Fingerprint: {incident.fingerprint}",
            f"Incident: {context['incident_url']}",
        )
        return EmailContent(
            notification_type="new_incident",
            subject=subject,
            recipients=self._recipients(),
            html_body=html,
            text_body=text,
            related_incident_id=incident.incident_id,
        )

    def _diagnosis_complete(self, payload: dict[str, Any]) -> EmailContent:
        incident = self._require_incident(str(payload.get("incident_id", "")))
        agent_run_id = str(payload.get("agent_run_id") or "")
        evidence = list(self.reads.list_evidence(incident.incident_id))[:5]
        root_cause = incident.root_cause_summary or "Root cause has not been determined"
        context = {
            "incident": incident,
            "root_cause": root_cause,
            "evidence": evidence,
            "incident_url": self._url(f"/incidents/{incident.incident_id}"),
            "run_url": self._url(f"/agent-runs/{agent_run_id}"),
            "report_url": self._url(f"/incidents/{incident.incident_id}/report"),
        }
        subject = f"[{incident.severity}] Diagnosis Complete: {incident.service}"
        html = self.email.render_html("diagnosis_complete.html", context)
        evidence_lines = [f"{item.evidence_id}: {item.title}" for item in evidence]
        text = _lines(
            subject,
            f"Root cause: {root_cause}",
            "Evidence:",
            *evidence_lines,
            f"Incident: {context['incident_url']}",
            f"Run: {context['run_url']}",
            f"Report: {context['report_url']}",
        )
        return EmailContent(
            notification_type="diagnosis_complete",
            subject=subject,
            recipients=self._recipients(),
            html_body=html,
            text_body=text,
            related_incident_id=incident.incident_id,
            related_agent_run_id=agent_run_id or None,
        )

    def _approval_request(self, payload: dict[str, Any]) -> EmailContent:
        approval_id = str(payload.get("approval_id", ""))
        approval = self.approvals.get_by_public_id(approval_id)
        if approval is None:
            raise ValueError(f"approval not found: {approval_id}")
        action = self.actions.get_by_public_id(approval.action_id)
        if action is None:
            raise ValueError(f"action not found: {approval.action_id}")
        incident = self._require_incident(approval.incident_id)
        is_l3 = action.risk_level == "L3"
        subject = (
            f"[CONFIRM] L3 Action: {action.type} on {action.target}"
            if is_l3
            else f"[ACTION REQUIRED] {action.risk_level} Approval: {action.type}"
        )

        # Generate or reuse email token for direct approve/reject links (L2 only, not L3)
        import secrets
        from datetime import timedelta

        from packages.common.time import utc_now

        email_token = None
        if not is_l3:
            now = utc_now()
            # Reuse existing valid token so re-sent emails don't invalidate earlier links
            if (
                approval.email_token is not None
                and approval.email_token_expires_at is not None
                and approval.email_token_expires_at > now
            ):
                email_token = approval.email_token
            else:
                email_token = secrets.token_urlsafe(24)
                approval.email_token = email_token
                approval.email_token_expires_at = now + timedelta(hours=24)
                self.db.flush()

        context = {
            "heading": subject,
            "approval": approval,
            "action": action,
            "incident": incident,
            "approval_url": self._url(f"/approvals/{approval.approval_id}"),
            "incident_url": self._url(f"/incidents/{incident.incident_id}"),
            "email_token": email_token,
            "approve_email_url": self._url(f"/api/approvals/by-token/{email_token}/approve") if email_token else "",
            "reject_email_url": self._url(f"/api/approvals/by-token/{email_token}/reject") if email_token else "",
        }
        html = self.email.render_html("approval_request.html", context)
        text_prefix = (
            f"Approve: {context['approve_email_url']}\nReject: {context['reject_email_url']}\n"
            if email_token
            else ""
        )
        text = _lines(
            subject,
            f"Service: {incident.service}",
            f"Action: {action.type}",
            f"Target: {action.target}",
            f"Risk: {action.risk_level}",
            f"Reason: {action.reason}",
            text_prefix + f"Approval: {context['approval_url']}",
        )

        # Combine global recipients with approval group members
        recipients = self._recipients()
        try:
            from packages.db.repositories.approval_groups import ApprovalGroupRepository

            groups = ApprovalGroupRepository(self.db)
            group = groups.find_by_service(incident.service)
            if group is not None:
                for member in group.members:
                    if member not in recipients:
                        recipients.append(member)
        except Exception:
            import logging
            logging.getLogger(__name__).warning(
                "failed to resolve approval group for service %s", incident.service, exc_info=True
            )

        return EmailContent(
            notification_type="approval_request",
            subject=subject,
            recipients=recipients,
            html_body=html,
            text_body=text,
            related_incident_id=incident.incident_id,
            related_agent_run_id=approval.agent_run_id,
            related_approval_id=approval.approval_id,
        )

    def _incident_report(self, payload: dict[str, Any]) -> EmailContent:
        report_id = str(payload.get("report_id", ""))
        report = self.reports.get_by_public_id(report_id)
        if report is None:
            raise ValueError(f"report not found: {report_id}")
        incident = self._require_incident(report.incident_id)
        context = {
            "report": report,
            "incident": incident,
            "report_url": self._url(f"/incidents/{incident.incident_id}/report"),
            "incident_url": self._url(f"/incidents/{incident.incident_id}"),
        }
        subject = f"[REPORT] Incident Report: {incident.incident_id}"
        html = self.email.render_html("incident_report.html", context)
        text = _lines(
            subject,
            report.body_markdown,
            f"Report: {context['report_url']}",
        )
        return EmailContent(
            notification_type="incident_report",
            subject=subject,
            recipients=self._recipients(),
            html_body=html,
            text_body=text,
            related_incident_id=incident.incident_id,
            related_agent_run_id=report.agent_run_id,
            related_report_id=report.report_id,
        )

    def _daily_summary(self, payload: dict[str, Any]) -> EmailContent:
        summary_date, start_utc, end_utc = self._summary_window(payload)
        stmt = (
            select(Incident)
            .where(Incident.created_at >= start_utc, Incident.created_at < end_utc)
            .order_by(Incident.created_at.desc(), Incident.id.desc())
        )
        incidents = list(self.db.scalars(stmt).all())
        context = {
            "summary_date": summary_date.isoformat(),
            "incidents": incidents,
            "incidents_url": self._url("/incidents"),
        }
        subject = "Daily Incident Summary"
        html = self.email.render_html("daily_summary.html", context)
        rows = [
            f"{item.incident_id} {item.service} {item.severity} {item.status} {item.alert_name}"
            for item in incidents
        ]
        text = _lines(subject, f"Date: {summary_date.isoformat()}", *rows, context["incidents_url"])
        return EmailContent(
            notification_type="daily_summary",
            subject=subject,
            recipients=self._recipients(),
            html_body=html,
            text_body=text,
        )

    def _require_incident(self, incident_id: str) -> Incident:
        incident = self.incidents.get_by_public_id(incident_id)
        if incident is None:
            raise ValueError(f"incident not found: {incident_id}")
        return incident

    def _summary_window(self, payload: dict[str, Any]) -> tuple[date, datetime, datetime]:
        tz_name = self.settings.notification_timezone or "UTC"
        try:
            tz = ZoneInfo(tz_name)
        except ZoneInfoNotFoundError:
            tz = ZoneInfo("UTC")
        raw_date = payload.get("date")
        if raw_date:
            summary_date = date.fromisoformat(str(raw_date))
        else:
            summary_date = utc_now().astimezone(tz).date()
        start_local = datetime.combine(summary_date, time.min, tzinfo=tz)
        end_local = datetime.combine(summary_date, time.max, tzinfo=tz)
        return summary_date, start_local.astimezone(UTC), end_local.astimezone(UTC)

    def _recipients(self) -> list[str]:
        return parse_recipients(self.settings.sre_email_list)

    def _url(self, path: str) -> str:
        base = self.settings.web_base_url.rstrip("/")
        return f"{base}{path}"


def parse_recipients(value: str) -> list[str]:
    return [item.strip() for item in re.split(r"[,;]", value) if item.strip()]


def _lines(*values: Any) -> str:
    return "\n".join(str(value) for value in values if value is not None and str(value) != "")
