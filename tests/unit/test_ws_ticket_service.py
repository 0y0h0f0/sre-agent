from __future__ import annotations

import pytest

from apps.api.services.ws_ticket_service import WebSocketTicketService
from packages.common.settings import Settings


def test_ws_ticket_verifies_for_matching_incident() -> None:
    settings = Settings(websocket_ticket_secret="test-ticket-secret")
    service = WebSocketTicketService(settings)

    issued = service.issue("inc_123", {"key_id": "apik_123"})
    payload = service.verify(issued.ticket, incident_id="inc_123")

    assert payload is not None
    assert payload["incident_id"] == "inc_123"
    assert payload["key_id"] == "apik_123"


def test_ws_ticket_rejects_wrong_incident() -> None:
    settings = Settings(websocket_ticket_secret="test-ticket-secret")
    service = WebSocketTicketService(settings)

    issued = service.issue("inc_123", {"key_id": "apik_123"})

    assert service.verify(issued.ticket, incident_id="inc_other") is None


def test_ws_ticket_requires_secret_in_production_with_auth() -> None:
    settings = Settings(app_env="production", api_key_auth_enabled=True)
    service = WebSocketTicketService(settings)

    with pytest.raises(RuntimeError, match="WEBSOCKET_TICKET_SECRET"):
        service.issue("inc_123", {"key_id": "apik_123"})
