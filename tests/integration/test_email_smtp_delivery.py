from __future__ import annotations

import socketserver
import threading
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from apps.api.services.email_service import EmailNotificationService
from packages.common.ids import new_id
from packages.common.settings import Settings
from packages.db.models import EmailLog, Incident


class _SMTPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(
        self, server_address: tuple[str, int], handler: type[socketserver.BaseRequestHandler]
    ):
        super().__init__(server_address, handler)
        self.messages: list[bytes] = []


class _SMTPHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        server = self.server
        assert isinstance(server, _SMTPServer)
        self.wfile.write(b"220 localhost ESMTP ready\r\n")
        in_data = False
        data: list[bytes] = []

        while True:
            line = self.rfile.readline()
            if not line:
                break
            command = line.decode("utf-8", errors="replace").strip()
            upper = command.upper()

            if in_data:
                if command == ".":
                    server.messages.append(b"".join(data))
                    data = []
                    in_data = False
                    self.wfile.write(b"250 2.0.0 queued\r\n")
                else:
                    data.append(line)
                continue

            if upper.startswith(("EHLO", "HELO")):
                self.wfile.write(b"250-localhost\r\n250 SIZE 35882577\r\n")
            elif upper.startswith("MAIL FROM"):
                self.wfile.write(b"250 2.1.0 ok\r\n")
            elif upper.startswith("RCPT TO"):
                self.wfile.write(b"250 2.1.5 ok\r\n")
            elif upper == "DATA":
                in_data = True
                self.wfile.write(b"354 end with <CR><LF>.<CR><LF>\r\n")
            elif upper == "RSET":
                self.wfile.write(b"250 2.0.0 reset\r\n")
            elif upper == "NOOP":
                self.wfile.write(b"250 2.0.0 ok\r\n")
            elif upper == "QUIT":
                self.wfile.write(b"221 2.0.0 bye\r\n")
                break
            else:
                self.wfile.write(b"250 2.0.0 ok\r\n")


@pytest.fixture()
def smtp_server() -> Any:
    server = _SMTPServer(("127.0.0.1", 0), _SMTPHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _incident(db: Session) -> Incident:
    incident = Incident(
        incident_id=new_id("inc_"),
        fingerprint="fp-email-smtp",
        source="mock",
        service="checkout-api",
        severity="P1",
        alert_name="High5xxAfterDeploy",
        status="open",
        starts_at=datetime(2026, 6, 1, 0, 0, tzinfo=UTC),
        labels={},
        annotations={"summary": "5xx spike"},
        raw_payload={"fingerprint": "fp-email-smtp"},
        root_cause_summary="Bad release caused elevated 5xx",
    )
    db.add(incident)
    return incident


def test_email_service_sends_to_local_smtp_server(db_session, smtp_server) -> None:
    pytest.importorskip("aiosmtplib")
    incident = _incident(db_session)
    db_session.commit()
    settings = Settings(
        database_url="sqlite+pysqlite:///:memory:",
        smtp_host="127.0.0.1",
        smtp_port=smtp_server.server_address[1],
        smtp_tls_mode="none",
        smtp_user=None,
        smtp_password=None,
        smtp_from="agent@example.com",
        sre_email_list="sre@example.com",
        web_base_url="http://console.local",
    )

    result = EmailNotificationService(db_session, settings).send_event(
        "new_incident", {"incident_id": incident.incident_id}
    )

    log = db_session.scalar(select(EmailLog).where(EmailLog.email_log_id == result["email_log_id"]))
    assert result["status"] == "sent"
    assert log is not None
    assert log.status == "sent"
    assert log.attempts == 1
    assert log.sent_at is not None
    assert len(smtp_server.messages) == 1
    delivered = smtp_server.messages[0].decode("utf-8", errors="replace")
    assert "Subject: [P1] New Incident: High5xxAfterDeploy" in delivered
    assert "To: sre@example.com" in delivered
    assert "Fingerprint: fp-email-smtp" in delivered
