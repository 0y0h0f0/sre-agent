from __future__ import annotations

import socket
import ssl

import pytest

from packages.common.settings import Settings


class RealEmailProbeSettings(Settings):
    run_real_email_test: bool = False


@pytest.mark.real_email
def test_smtp_tcp_connectivity() -> None:
    settings = RealEmailProbeSettings()
    if not settings.run_real_email_test:
        pytest.skip("set RUN_REAL_EMAIL_TEST=true to probe real SMTP connectivity")
    if not settings.smtp_host:
        pytest.skip("missing SMTP_HOST")

    address = (settings.smtp_host, settings.smtp_port)
    timeout = settings.smtp_timeout_seconds
    try:
        raw_sock = socket.create_connection(address, timeout=timeout)
    except OSError as exc:
        pytest.fail(
            f"cannot connect to {settings.smtp_host}:{settings.smtp_port} "
            f"within {timeout}s: {exc}. This is network/firewall/port reachability, "
            "not SMTP username/password validation."
        )

    with raw_sock:
        mode = settings.smtp_tls_mode.strip().lower()
        if mode == "tls" or (mode == "auto" and settings.smtp_port == 465):
            context = ssl.create_default_context()
            with context.wrap_socket(raw_sock, server_hostname=settings.smtp_host) as tls_sock:
                tls_sock.settimeout(timeout)
                banner = tls_sock.recv(512)
        else:
            raw_sock.settimeout(timeout)
            banner = raw_sock.recv(512)

    assert banner.startswith(b"220"), banner.decode("utf-8", errors="replace")
