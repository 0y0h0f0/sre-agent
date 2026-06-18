"""Shared Kubernetes targeting helpers for agent nodes."""

from __future__ import annotations

from typing import Any


def effective_executor_k8s_namespace(settings: Any) -> str:
    """Return the namespace used by live executor, snapshot, and verify paths."""
    return (
        str(getattr(settings, "executor_k8s_namespace", "") or "").strip()
        or str(getattr(settings, "k8s_namespace", "") or "").strip()
        or "default"
    )
