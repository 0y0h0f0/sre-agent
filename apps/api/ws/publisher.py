"""Publish node/incident/approval events to Redis Pub/Sub.

The Celery worker calls publish_event() after each node transition;
the WebSocket endpoint subscribes to the same channel and forwards
events to the browser client.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

import redis

from packages.common.settings import get_settings

logger = logging.getLogger(__name__)


def _redis_client() -> redis.Redis:
    settings = get_settings()
    return redis.Redis.from_url(
        settings.redis_url,
        socket_connect_timeout=settings.redis_socket_connect_timeout,
        socket_timeout=settings.redis_socket_timeout,
        retry_on_timeout=settings.redis_retry_on_timeout,
    )


def publish_event(incident_id: str, event_type: str, payload: dict[str, Any]) -> None:
    """Publish a JSON event to the Redis channel for the given incident."""
    try:
        client = _redis_client()
        event = {
            "type": event_type,
            "payload": payload,
            "timestamp": datetime.now(UTC).isoformat(),
        }
        client.publish(f"incident:{incident_id}", json.dumps(event, default=str))
    except Exception:
        # Never let pub/sub failures crash the worker
        logger.warning("failed to publish %s event for incident %s", event_type, incident_id, exc_info=True)


def publish_node_event(
    incident_id: str,
    agent_run_id: str,
    node_name: str,
    status: str,
    **kwargs: Any,
) -> None:
    """Convenience wrapper for publishing a node_update event."""
    payload: dict[str, Any] = {
        "agent_run_id": agent_run_id,
        "node_name": node_name,
        "status": status,
    }
    payload.update(kwargs)
    publish_event(incident_id, "node_update", payload)


def publish_approval_event(
    incident_id: str,
    approval_id: str,
    status: str,
    **kwargs: Any,
) -> None:
    """Convenience wrapper for publishing an approval_update event."""
    payload: dict[str, Any] = {
        "approval_id": approval_id,
        "status": status,
    }
    payload.update(kwargs)
    publish_event(incident_id, "approval_update", payload)
