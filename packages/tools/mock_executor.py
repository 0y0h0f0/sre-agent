"""Shared mock executor result map — single source of truth.

Used by both the graph ``execute_action`` node and the API ``ActionService``
to ensure consistent mock behavior regardless of execution path.
"""

from __future__ import annotations

MOCK_EXECUTOR_RESULTS: dict[str, dict[str, str]] = {
    "restart_pod": {"status": "succeeded", "message": "mock pod restart completed"},
    "restart_service": {"status": "succeeded", "message": "mock service restart completed"},
    "restart_statefulset": {
        "status": "succeeded",
        "message": "mock statefulset restart completed",
    },
    "pause_rollout": {"status": "succeeded", "message": "mock rollout pause completed"},
    "resume_rollout": {"status": "succeeded", "message": "mock rollout resume completed"},
    "scale_deployment": {"status": "succeeded", "message": "mock scaling completed"},
    "rollback_release": {"status": "succeeded", "message": "mock rollback completed"},
    "enable_rate_limit": {"status": "succeeded", "message": "mock rate limit enabled"},
    "warmup_cache": {"status": "succeeded", "message": "mock cache warming completed"},
    "create_ticket": {"status": "succeeded", "message": "mock ticket created"},
    "adjust_connection_pool": {"status": "succeeded", "message": "mock pool adjusted"},
    "increase_memory_limit": {
        "status": "succeeded",
        "message": "mock memory limit increase completed",
    },
}
