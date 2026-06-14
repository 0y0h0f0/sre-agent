"""Deterministic action capability registry.

This module is intentionally metadata-only. PR 1 introduces the contract that
later execution nodes can enforce, without changing guardrail or executor
behavior yet.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Literal

from pydantic import BaseModel, ConfigDict

ActionCategory = Literal[
    "read_only",
    "record_only",
    "local_or_fixture_only",
    "live_mutating_reversible",
    "live_mutating_bounded_irreversible",
    "forbidden",
]
LiveBackend = Literal["none", "k8s"]

DB_READ_ONLY_DIAGNOSTIC_OPERATIONS: tuple[str, ...] = (
    "connection_pool",
    "locks",
    "slow_queries",
)


class ActionCapability(BaseModel):
    """Static execution capability metadata for a planned action type."""

    model_config = ConfigDict(frozen=True)

    action_type: str
    category: ActionCategory
    live_backend: LiveBackend = "none"
    reversible: bool = False
    bounded_irreversible: bool = False
    rollback_action_type: str | None = None
    required_snapshot_paths: tuple[str, ...] = ()
    optional_snapshot_paths: tuple[str, ...] = ()
    preflight_checks: tuple[str, ...] = ()
    verify_gates: tuple[str, ...] = ()
    risk_level_expectation: str | None = None


def _cap(
    action_type: str,
    category: ActionCategory,
    *,
    live_backend: LiveBackend = "none",
    reversible: bool = False,
    bounded_irreversible: bool = False,
    rollback_action_type: str | None = None,
    required_snapshot_paths: tuple[str, ...] = (),
    optional_snapshot_paths: tuple[str, ...] = (),
    preflight_checks: tuple[str, ...] = (),
    verify_gates: tuple[str, ...] = (),
    risk_level_expectation: str | None = None,
) -> ActionCapability:
    return ActionCapability(
        action_type=action_type,
        category=category,
        live_backend=live_backend,
        reversible=reversible,
        bounded_irreversible=bounded_irreversible,
        rollback_action_type=rollback_action_type,
        required_snapshot_paths=required_snapshot_paths,
        optional_snapshot_paths=optional_snapshot_paths,
        preflight_checks=preflight_checks,
        verify_gates=verify_gates,
        risk_level_expectation=risk_level_expectation,
    )


_K8S_DEPLOYMENT_IDENTITY = ("k8s.name", "k8s.namespace")
_K8S_REPLICA_SNAPSHOT = (
    *_K8S_DEPLOYMENT_IDENTITY,
    "k8s.replicas",
)
_K8S_RESTART_SNAPSHOT = (
    *_K8S_REPLICA_SNAPSHOT,
    "k8s.ready_replicas",
    "k8s.available_replicas",
    "k8s.image",
)
_K8S_RESTART_PREFLIGHT = (
    "k8s_target_name_valid",
    "k8s_namespace_valid",
    "k8s_deployment_exists",
    "k8s_replicas_gt_zero",
    "k8s_rollout_not_failed",
    "k8s_rolling_restart_patch_only",
)
_K8S_VERIFY = ("k8s_rollout", "metrics_logs")

_CAPABILITIES: dict[str, ActionCapability] = {
    "query_metrics": _cap(
        "query_metrics",
        "read_only",
        risk_level_expectation="L0",
    ),
    "query_logs": _cap(
        "query_logs",
        "read_only",
        risk_level_expectation="L0",
    ),
    "query_traces": _cap(
        "query_traces",
        "read_only",
        risk_level_expectation="L0",
    ),
    "query_git": _cap(
        "query_git",
        "read_only",
        risk_level_expectation="L0",
    ),
    "create_ticket": _cap(
        "create_ticket",
        "record_only",
        risk_level_expectation="L1",
    ),
    "generate_report": _cap(
        "generate_report",
        "record_only",
        risk_level_expectation="L1",
    ),
    "warmup_cache": _cap(
        "warmup_cache",
        "local_or_fixture_only",
        risk_level_expectation="L1",
    ),
    "adjust_connection_pool": _cap(
        "adjust_connection_pool",
        "local_or_fixture_only",
        risk_level_expectation="L1",
    ),
    "scale_connection_pool": _cap(
        "scale_connection_pool",
        "local_or_fixture_only",
        risk_level_expectation="L2",
    ),
    "kill_idle_transactions": _cap(
        "kill_idle_transactions",
        "forbidden",
        risk_level_expectation="L4",
    ),
    "restart_pod": _cap(
        "restart_pod",
        "live_mutating_bounded_irreversible",
        live_backend="k8s",
        bounded_irreversible=True,
        required_snapshot_paths=_K8S_RESTART_SNAPSHOT,
        optional_snapshot_paths=("k8s.revision",),
        preflight_checks=_K8S_RESTART_PREFLIGHT,
        verify_gates=_K8S_VERIFY,
        risk_level_expectation="L2",
    ),
    "restart_service": _cap(
        "restart_service",
        "live_mutating_bounded_irreversible",
        live_backend="k8s",
        bounded_irreversible=True,
        required_snapshot_paths=_K8S_RESTART_SNAPSHOT,
        optional_snapshot_paths=("k8s.revision",),
        preflight_checks=_K8S_RESTART_PREFLIGHT,
        verify_gates=_K8S_VERIFY,
        risk_level_expectation="L2",
    ),
    "scale_deployment": _cap(
        "scale_deployment",
        "live_mutating_reversible",
        live_backend="k8s",
        reversible=True,
        rollback_action_type="scale_back",
        required_snapshot_paths=_K8S_REPLICA_SNAPSHOT,
        verify_gates=_K8S_VERIFY,
        risk_level_expectation="L2",
    ),
    "scale_back": _cap(
        "scale_back",
        "live_mutating_reversible",
        live_backend="k8s",
        reversible=True,
        rollback_action_type="scale_deployment",
        required_snapshot_paths=_K8S_REPLICA_SNAPSHOT,
        verify_gates=_K8S_VERIFY,
        risk_level_expectation="L2",
    ),
    "rollback_release": _cap(
        "rollback_release",
        "live_mutating_reversible",
        live_backend="k8s",
        reversible=True,
        rollback_action_type="rollback_release",
        required_snapshot_paths=(
            *_K8S_DEPLOYMENT_IDENTITY,
            "k8s.revision",
            "k8s.image",
        ),
        verify_gates=("k8s_rollout", "metrics_logs", "db_readonly"),
        risk_level_expectation="L3",
    ),
    "rollback_deployment": _cap(
        "rollback_deployment",
        "live_mutating_reversible",
        live_backend="k8s",
        reversible=True,
        rollback_action_type="rollback_release",
        required_snapshot_paths=(
            *_K8S_DEPLOYMENT_IDENTITY,
            "k8s.revision",
            "k8s.image",
        ),
        verify_gates=("k8s_rollout", "metrics_logs", "db_readonly"),
        risk_level_expectation="L3",
    ),
    "enable_rate_limit": _cap(
        "enable_rate_limit",
        "local_or_fixture_only",
        risk_level_expectation="L3",
    ),
    "raise_rate_limit": _cap(
        "raise_rate_limit",
        "local_or_fixture_only",
        risk_level_expectation="L3",
    ),
    "enable_circuit_breaker": _cap(
        "enable_circuit_breaker",
        "local_or_fixture_only",
        risk_level_expectation="L3",
    ),
    "switch_dns_resolver": _cap(
        "switch_dns_resolver",
        "local_or_fixture_only",
        risk_level_expectation="L3",
    ),
    "failover": _cap(
        "failover",
        "local_or_fixture_only",
        risk_level_expectation="L3",
    ),
    "revert_config": _cap(
        "revert_config",
        "local_or_fixture_only",
        risk_level_expectation="L2",
    ),
    "cancel_deployment": _cap(
        "cancel_deployment",
        "local_or_fixture_only",
        risk_level_expectation="L3",
    ),
    "scale_cache": _cap(
        "scale_cache",
        "local_or_fixture_only",
        risk_level_expectation="L2",
    ),
    "increase_memory_limit": _cap(
        "increase_memory_limit",
        "local_or_fixture_only",
        risk_level_expectation="L2",
    ),
    "scale_disk": _cap(
        "scale_disk",
        "local_or_fixture_only",
        risk_level_expectation="L2",
    ),
    "rotate_logs": _cap(
        "rotate_logs",
        "local_or_fixture_only",
        risk_level_expectation="L2",
    ),
    "renew_certificate": _cap(
        "renew_certificate",
        "local_or_fixture_only",
        risk_level_expectation="L2",
    ),
    "scale_consumers": _cap(
        "scale_consumers",
        "local_or_fixture_only",
        risk_level_expectation="L2",
    ),
    "enable_caching": _cap(
        "enable_caching",
        "local_or_fixture_only",
        risk_level_expectation="L2",
    ),
    "delete_data": _cap(
        "delete_data",
        "forbidden",
        risk_level_expectation="L4",
    ),
    "truncate_table": _cap(
        "truncate_table",
        "forbidden",
        risk_level_expectation="L4",
    ),
    "flush_cache": _cap(
        "flush_cache",
        "forbidden",
        risk_level_expectation="L4",
    ),
    "modify_database": _cap(
        "modify_database",
        "forbidden",
        risk_level_expectation="L4",
    ),
}

ACTION_CAPABILITIES = MappingProxyType(_CAPABILITIES)


def get_action_capability(action_type: str) -> ActionCapability | None:
    """Return capability metadata for ``action_type`` if it is registered."""

    return ACTION_CAPABILITIES.get(action_type.strip().lower())


def iter_action_capabilities() -> tuple[ActionCapability, ...]:
    """Return all registered capabilities in deterministic action-type order."""

    return tuple(ACTION_CAPABILITIES[key] for key in sorted(ACTION_CAPABILITIES))


def capabilities_by_category(category: ActionCategory) -> tuple[ActionCapability, ...]:
    """Return capabilities in ``category`` in deterministic action-type order."""

    return tuple(
        capability for capability in iter_action_capabilities() if capability.category == category
    )


def known_action_types() -> frozenset[str]:
    """Return all registered action types."""

    return frozenset(ACTION_CAPABILITIES)
