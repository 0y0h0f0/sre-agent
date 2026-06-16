"""Short-lived WebSocket tickets.

The browser cannot attach an Authorization header to the WebSocket handshake.
Use a short-lived, incident-scoped ticket instead of placing the long-lived API
key in the URL query string.
"""

from __future__ import annotations

import base64
import hmac
import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from hashlib import sha256
from typing import Any

from packages.common.settings import Settings
from packages.common.time import utc_now

_LOCAL_TICKET_SECRET = secrets.token_bytes(32)


@dataclass(frozen=True)
class WebSocketTicket:
    ticket: str
    expires_at: datetime


class WebSocketTicketService:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def issue(self, incident_id: str, identity: dict[str, Any]) -> WebSocketTicket:
        expires_at = utc_now() + timedelta(
            seconds=self._settings.websocket_ticket_ttl_seconds
        )
        payload = {
            "incident_id": incident_id,
            "key_id": str(identity.get("key_id") or "anonymous"),
            "exp": int(expires_at.timestamp()),
            "nonce": secrets.token_urlsafe(16),
        }
        body = _b64(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
        signature = _b64(
            hmac.new(self._secret(), body.encode(), sha256).digest()
        )
        return WebSocketTicket(ticket=f"{body}.{signature}", expires_at=expires_at)

    def verify(self, ticket: str, *, incident_id: str) -> dict[str, Any] | None:
        try:
            body, signature = ticket.split(".", 1)
        except ValueError:
            return None

        expected = _b64(hmac.new(self._secret(), body.encode(), sha256).digest())
        if not hmac.compare_digest(signature, expected):
            return None

        try:
            payload = json.loads(_unb64(body).decode())
        except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
            return None

        if not isinstance(payload, dict):
            return None
        if payload.get("incident_id") != incident_id:
            return None
        exp = payload.get("exp")
        if not isinstance(exp, int) or exp < int(utc_now().timestamp()):
            return None
        return payload

    def _secret(self) -> bytes:
        configured = self._settings.websocket_ticket_secret
        if configured is not None:
            return configured.get_secret_value().encode()
        if (
            self._settings.app_env == "production"
            and self._settings.api_key_auth_enabled
        ):
            raise RuntimeError(
                "WEBSOCKET_TICKET_SECRET is required when API key auth is "
                "enabled in production"
            )
        return _LOCAL_TICKET_SECRET


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def _unb64(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)
