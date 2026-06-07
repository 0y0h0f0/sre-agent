"""WebSocket endpoint for real-time incident diagnosis updates.

Clients connect to /api/ws/incidents/{incident_id} and receive
JSON events published by the Celery worker via Redis Pub/Sub.

Authentication is via ``?token=<api_key>`` query parameter when
``api_key_auth_enabled`` is True (Phase 7.1).
"""

from __future__ import annotations

import json
import logging

import redis
from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from apps.api.services.api_key_service import ApiKeyService
from packages.common.settings import get_settings
from packages.db.session import SessionLocal

logger = logging.getLogger(__name__)

router = APIRouter()


@router.websocket("/api/ws/incidents/{incident_id}")
async def incident_events(
    websocket: WebSocket,
    incident_id: str,
    token: str = Query(default=""),
) -> None:
    settings = get_settings()

    if settings.api_key_auth_enabled:
        if not token:
            await websocket.close(code=4001, reason="missing token")
            return
        db = SessionLocal()
        try:
            identity = ApiKeyService(db).verify(token)
            if identity is None:
                await websocket.close(code=4001, reason="invalid token")
                return
        finally:
            db.close()

    await websocket.accept()

    pubsub: redis.client.PubSub | None = None
    client: redis.Redis | None = None

    try:
        client = redis.Redis.from_url(
            settings.redis_url,
            socket_connect_timeout=settings.redis_socket_connect_timeout,
            socket_timeout=settings.redis_socket_timeout,
            retry_on_timeout=settings.redis_retry_on_timeout,
        )
        pubsub = client.pubsub()
        pubsub.subscribe(f"incident:{incident_id}")

        await websocket.send_json({"type": "connected", "incident_id": incident_id})

        for message in pubsub.listen():
            if message["type"] != "message":
                continue
            if message.get("data") is None:
                continue

            try:
                raw = message["data"]
                data = json.loads(raw) if isinstance(raw, bytes) else raw
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
                pubsub.unsubscribe(f"incident:{incident_id}")
            except Exception:
                logger.warning(
                    "failed to unsubscribe pubsub for incident %s", incident_id, exc_info=True
                )
            try:
                pubsub.close()
            except Exception:
                logger.warning(
                    "failed to close pubsub for incident %s", incident_id, exc_info=True
                )
        if client is not None:
            try:
                client.close()
            except Exception:
                logger.warning(
                    "failed to close redis client for incident %s", incident_id, exc_info=True
                )
