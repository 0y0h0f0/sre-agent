from __future__ import annotations

from datetime import UTC, datetime

import pytest

from apps.api.services.email_service import (
    EmailContent,
    EmailSendError,
    EmailService,
    parse_recipients,
)
from packages.common.settings import Settings


class RealEmailTestSettings(Settings):
    run_real_email_test: bool = False


def _missing_config(settings: RealEmailTestSettings) -> list[str]:
    missing: list[str] = []
    if not settings.smtp_host:
        missing.append("SMTP_HOST")
    if not parse_recipients(settings.sre_email_list):
        missing.append("SRE_EMAIL_LIST")
    if not settings.smtp_from or settings.smtp_from == "sre-agent@example.local":
        missing.append("SMTP_FROM")
    return missing


@pytest.mark.real_email
def test_send_real_email_via_configured_smtp() -> None:
    pytest.importorskip("aiosmtplib")
    settings = RealEmailTestSettings()
    if not settings.run_real_email_test:
        pytest.skip("set RUN_REAL_EMAIL_TEST=true to send a real SMTP email")

    missing = _missing_config(settings)
    if missing:
        pytest.skip("missing real email config: " + ", ".join(missing))

    now = datetime.now(UTC).isoformat(timespec="seconds")
    recipients = parse_recipients(settings.sre_email_list)
    subject = f"[SRE Agent] Real SMTP smoke test {now}"
    text = (
        f"{subject}\n"
        "This message was sent by tests/manual/test_real_email_delivery.py.\n"
        f"Recipients: {', '.join(recipients)}\n"
        f"Console: {settings.web_base_url}\n"
    )
    html = (
        "<html><body>"
        f"<h1>{subject}</h1>"
        "<p>This message was sent by the SRE Agent real SMTP smoke test.</p>"
        f'<p>Console: <a href="{settings.web_base_url}">{settings.web_base_url}</a></p>'
        "</body></html>"
    )

    try:
        provider_message_id = EmailService(settings).send_sync(
            EmailContent(
                notification_type="real_email_smoke",
                subject=subject,
                recipients=recipients,
                html_body=html,
                text_body=text,
            )
        )
    except EmailSendError as exc:
        pytest.fail(_smtp_failure_hint(settings, exc))

    assert provider_message_id is None or isinstance(provider_message_id, str)


def _smtp_failure_hint(settings: RealEmailTestSettings, exc: EmailSendError) -> str:
    message = str(exc)
    hint = (
        f"SMTP send failed: {message}\n"
        f"Configured endpoint: {settings.smtp_host}:{settings.smtp_port} "
        f"tls_mode={settings.smtp_tls_mode} timeout={settings.smtp_timeout_seconds}s\n"
    )
    if "Timed out connecting" in message:
        hint += (
            "This is a TCP connectivity timeout, before authentication. "
            "Check outbound SMTP port access. For Gmail, try "
            "SMTP_PORT=465 and SMTP_TLS_MODE=tls/auto, or test from a network "
            "that allows direct SMTP egress.\n"
        )
    return hint
