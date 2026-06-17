from __future__ import annotations

import pytest

from packages.agent.actions.capabilities import (
    ACTION_CAPABILITIES,
    DB_READ_ONLY_DIAGNOSTIC_OPERATIONS,
    capabilities_by_category,
    get_action_capability,
    iter_action_capabilities,
    known_action_types,
)
from packages.agent.guardrails.policy import _RISK_TABLE, classify_risk_level
from packages.agent.rules_fallback import _ACTIONS_MAP
from packages.tools.db_diagnostics import DbDiagnosticsQuery


def test_every_guardrail_action_has_capability() -> None:
    assert frozenset(_RISK_TABLE).issubset(known_action_types())


def test_every_fallback_action_has_capability() -> None:
    fallback_action_types = {
        action["type"] for actions in _ACTIONS_MAP.values() for action in actions
    }
    assert fallback_action_types.issubset(known_action_types())


def test_fallback_actions_do_not_recommend_l4_remediation() -> None:
    for alert_name, actions in _ACTIONS_MAP.items():
        for action in actions:
            decision = classify_risk_level(action)
            assert decision.risk_level != "L4", (
                f"{alert_name} fallback emitted forbidden action {action['type']}"
            )
            assert decision.allowed is True


def test_fallback_scale_deployment_means_replica_scale_only() -> None:
    for alert_name, actions in _ACTIONS_MAP.items():
        for action in actions:
            if action["type"] != "scale_deployment":
                continue
            params = action.get("params", {})
            assert set(params) == {"replicas"}, (
                f"{alert_name} uses scale_deployment for non-replica params: {params}"
            )
            assert isinstance(params["replicas"], int)
            assert params["replicas"] > 0


def test_memory_limit_fallback_uses_dedicated_action_type() -> None:
    memory_actions = _ACTIONS_MAP["MemoryLeak"]
    assert memory_actions[0]["type"] == "increase_memory_limit"
    assert set(memory_actions[0]["params"]) == {"memory_limit"}


def test_capability_risk_expectations_match_guardrail_policy() -> None:
    for capability in iter_action_capabilities():
        decision = classify_risk_level(
            {"type": capability.action_type, "target": "svc", "params": {}}
        )
        assert capability.risk_level_expectation == decision.risk_level
        assert decision.requires_approval is (decision.risk_level in {"L2", "L3"})


def test_l3_capabilities_are_explicitly_guarded() -> None:
    l3_action_types = {
        capability.action_type
        for capability in iter_action_capabilities()
        if capability.risk_level_expectation == "L3"
    }
    assert l3_action_types
    assert l3_action_types.issubset(_RISK_TABLE)


def test_l3_capabilities_require_second_confirmation_without_risk_hint() -> None:
    for capability in iter_action_capabilities():
        if capability.risk_level_expectation != "L3":
            continue
        decision = classify_risk_level(
            {
                "type": capability.action_type,
                "target": "svc",
                "params": {},
                "risk_hint": "L0",
            }
        )
        assert decision.risk_level == "L3"
        assert decision.requires_approval is True


def test_live_reversible_capabilities_have_rollback_snapshot_and_verify_contracts() -> None:
    capabilities = capabilities_by_category("live_mutating_reversible")
    assert {cap.action_type for cap in capabilities} == {
        "rollback_deployment",
        "rollback_release",
        "scale_back",
        "scale_deployment",
    }

    for capability in capabilities:
        assert capability.live_backend == "k8s"
        assert capability.reversible is True
        assert capability.bounded_irreversible is False
        assert capability.rollback_action_type in ACTION_CAPABILITIES
        assert capability.required_snapshot_paths
        assert capability.verify_gates


@pytest.mark.parametrize("action_type", ["restart_pod", "restart_service", "pause_rollout"])
def test_bounded_irreversible_k8s_actions_are_not_reversible(action_type: str) -> None:
    capability = get_action_capability(action_type)
    assert capability is not None
    assert capability.category == "live_mutating_bounded_irreversible"
    assert capability.live_backend == "k8s"
    assert capability.reversible is False
    assert capability.bounded_irreversible is True
    assert capability.rollback_action_type is None
    assert "k8s.replicas" in capability.required_snapshot_paths
    assert "k8s.image" in capability.required_snapshot_paths
    assert "k8s_deployment_exists" in capability.preflight_checks
    assert "k8s_rollout" in capability.verify_gates
    assert "metrics_logs" in capability.verify_gates
    assert capability.risk_level_expectation == "L2"


def test_pause_rollout_does_not_require_ready_replica_snapshot() -> None:
    capability = get_action_capability("pause_rollout")
    assert capability is not None
    assert "k8s.ready_replicas" not in capability.required_snapshot_paths
    assert "k8s.available_replicas" not in capability.required_snapshot_paths
    assert "k8s_rollout_pause_patch_only" in capability.preflight_checks


@pytest.mark.parametrize(
    "action_type",
    [
        "delete_data",
        "truncate_table",
        "flush_cache",
        "modify_database",
        "kill_idle_transactions",
    ],
)
def test_destructive_data_and_database_actions_are_forbidden(action_type: str) -> None:
    capability = get_action_capability(action_type)
    assert capability is not None
    assert capability.category == "forbidden"
    assert capability.live_backend == "none"
    assert capability.reversible is False
    assert capability.bounded_irreversible is False
    assert capability.risk_level_expectation == "L4"


def test_db_diagnostic_operations_are_read_only_only() -> None:
    assert set(DB_READ_ONLY_DIAGNOSTIC_OPERATIONS) == {
        "connection_pool",
        "locks",
        "slow_queries",
    }
    for operation in DB_READ_ONLY_DIAGNOSTIC_OPERATIONS:
        assert DbDiagnosticsQuery(operation=operation).operation == operation


def test_live_capability_policy_has_no_database_write_backend() -> None:
    for capability in iter_action_capabilities():
        assert capability.live_backend in {"none", "k8s"}
        if "database" in capability.action_type or "transaction" in capability.action_type:
            assert capability.live_backend == "none"
    modify_database = get_action_capability("modify_database")
    kill_idle_transactions = get_action_capability("kill_idle_transactions")
    assert modify_database is not None
    assert kill_idle_transactions is not None
    assert modify_database.category == "forbidden"
    assert kill_idle_transactions.category == "forbidden"


@pytest.mark.parametrize(
    "action_type",
    [
        "scale_connection_pool",
        "scale_cache",
        "enable_circuit_breaker",
        "increase_memory_limit",
        "scale_disk",
        "rotate_logs",
        "renew_certificate",
        "switch_dns_resolver",
        "scale_consumers",
        "raise_rate_limit",
        "enable_caching",
        "failover",
    ],
)
def test_fallback_only_actions_do_not_get_live_backend(action_type: str) -> None:
    capability = get_action_capability(action_type)
    assert capability is not None
    assert capability.category == "local_or_fixture_only"
    assert capability.live_backend == "none"
    assert capability.reversible is False
    assert capability.bounded_irreversible is False


def test_lookup_normalizes_action_type() -> None:
    assert get_action_capability(" Restart_Pod ") == get_action_capability("restart_pod")


def test_capability_iteration_is_deterministic() -> None:
    action_types = [capability.action_type for capability in iter_action_capabilities()]
    assert action_types == sorted(action_types)
