"""WebSocket endpoint for real-time incident diagnosis updates.

Clients connect to /api/ws/incidents/{incident_id} and receive
JSON events published by the Celery worker via Redis Pub/Sub.

Authentication is via a short-lived ``?ticket=...`` query parameter when
``api_key_auth_enabled`` is True (Phase 7.1). The ticket is issued by an HTTP
endpoint authenticated with the normal Authorization header.
"""

from __future__ import annotations

import json
import logging

import redis.asyncio as redis
from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from apps.api.dependencies import get_app_settings, get_current_api_key
from apps.api.services.ws_ticket_service import WebSocketTicketService
from packages.common.settings import Settings, get_settings

logger = logging.getLogger(__name__)

router = APIRouter()


class WebSocketTicketResponse(BaseModel):
    ticket: str
    expires_at: str


@router.post(
    "/api/ws/incidents/{incident_id}/ticket",
    response_model=WebSocketTicketResponse,
)
def create_incident_ws_ticket(
    incident_id: str,
    identity: dict[str, object] = Depends(get_current_api_key),
    settings: Settings = Depends(get_app_settings),
) -> WebSocketTicketResponse:
    if settings.api_key_auth_enabled and not identity:
        raise HTTPException(status_code=401, detail="Authentication required")
    try:
        issued = WebSocketTicketService(settings).issue(incident_id, identity)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return WebSocketTicketResponse(
        ticket=issued.ticket,
        expires_at=issued.expires_at.isoformat(),
    )


@router.websocket("/api/ws/incidents/{incident_id}")
async def incident_events(
    websocket: WebSocket,
    incident_id: str,
    ticket: str = Query(default=""),
) -> None:
    settings = get_settings()

    if settings.api_key_auth_enabled:
        if not ticket:
            await websocket.close(code=4001, reason="missing ticket")
            return
        try:
            payload = WebSocketTicketService(settings).verify(
                ticket,
                incident_id=incident_id,
            )
        except RuntimeError:
            await websocket.close(code=1011, reason="ticket validation unavailable")
            return
        if payload is None:
            await websocket.close(code=4001, reason="invalid ticket")
            return

    await websocket.accept()

    pubsub: object | None = None
    client: redis.Redis | None = None

    try:
        client = redis.Redis.from_url(
            settings.redis_url,
            socket_connect_timeout=settings.redis_socket_connect_timeout,
            socket_timeout=settings.redis_socket_timeout,
            retry_on_timeout=settings.redis_retry_on_timeout,
        )
        pubsub = client.pubsub()
        await pubsub.subscribe(f"incident:{incident_id}")  # type: ignore[attr-defined]

        await websocket.send_json({"type": "connected", "incident_id": incident_id})

        while True:
            message = await pubsub.get_message(  # type: ignore[attr-defined]
                ignore_subscribe_messages=True,
                timeout=1.0,
            )
            if message is None:
                continue
            if message.get("type") != "message":
                continue
            if message.get("data") is None:
                continue

            try:
                raw = message["data"]
                if isinstance(raw, bytes):
                    data = json.loads(raw.decode())
                elif isinstance(raw, str):
                    data = json.loads(raw)
                else:
                    data = raw
            except (json.JSONDecodeError, TypeError):
                continue

            await websocket.send_json(data)

    except WebSocketDisconnect:
        logger.info("websocket client disconnected for incident %s", incident_id)
    except Exception:
        logger.warning("websocket error for incident %s", incident_id, exc_info=True)
    finally:
        if pubsub is not None:
            try:
                await pubsub.unsubscribe(f"incident:{incident_id}")  # type: ignore[attr-defined]
            except Exception:
                logger.warning(
                    "failed to unsubscribe pubsub for incident %s", incident_id, exc_info=True
                )
            try:
                await pubsub.aclose()  # type: ignore[attr-defined]
            except Exception:
                logger.warning(
                    "failed to close pubsub for incident %s", incident_id, exc_info=True
                )
        if client is not None:
            try:
                await client.aclose()
            except Exception:
                logger.warning(
                    "failed to close redis client for incident %s", incident_id, exc_info=True
                )
