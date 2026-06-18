"""Comprehensive unit tests for guardrail risk classification.

Target: >=95% coverage of packages/agent/guardrails/policy.py.
"""

from __future__ import annotations

import pytest

from packages.agent.guardrails.policy import classify_risk_level
from packages.agent.schemas import GuardrailDecision

# ---------------------------------------------------------------------------
# Parameterized: every known action type
# ---------------------------------------------------------------------------

_RISK_MATRIX = [
    # L0: read-only, auto
    ("query_metrics", "L0", False, True),
    ("query_logs", "L0", False, True),
    ("query_traces", "L0", False, True),
    ("query_git", "L0", False, True),
    # L1: low-risk write, auto
    ("create_ticket", "L1", False, True),
    ("generate_report", "L1", False, True),
    ("warmup_cache", "L1", False, True),
    ("adjust_connection_pool", "L1", False, True),
    # L2: approval required
    ("restart_pod", "L2", True, True),
    ("restart_deployment", "L2", True, True),
    ("scale_deployment", "L2", True, True),
    ("restart_service", "L2", True, True),
    ("restart_statefulset", "L2", True, True),
    ("pause_rollout", "L2", True, True),
    ("resume_rollout", "L2", True, True),
    ("increase_memory_limit", "L2", True, True),
    # L3: approval + secondary confirmation
    ("enable_rate_limit", "L3", True, True),
    ("raise_rate_limit", "L3", True, True),
    ("rollback_release", "L3", True, True),
    ("rollback_deployment", "L3", True, True),
    ("enable_circuit_breaker", "L3", True, True),
    ("switch_dns_resolver", "L3", True, True),
    ("failover", "L3", True, True),
    ("scale_back", "L2", True, True),
    ("revert_config", "L2", True, True),
    ("cancel_deployment", "L3", True, True),
    # L4: always blocked
    ("kill_idle_transactions", "L4", False, False),
    ("delete_data", "L4", False, False),
    ("truncate_table", "L4", False, False),
    ("flush_cache", "L4", False, False),
    ("modify_database", "L4", False, False),
]


@pytest.mark.parametrize(
    "action_type,expected_level,expects_approval,expects_allowed", _RISK_MATRIX
)
def test_risk_matrix(action_type, expected_level, expects_approval, expects_allowed):
    decision = classify_risk_level({"type": action_type, "target": "svc", "params": {}})
    assert decision.risk_level == expected_level
    assert decision.requires_approval == expects_approval
    assert decision.allowed == expects_allowed


# ---------------------------------------------------------------------------
# Forbidden keyword detection
# ---------------------------------------------------------------------------

_FORBIDDEN_KEYWORDS = ["delete", "drop", "truncate", "modify_database", "flush"]


@pytest.mark.parametrize("kw", _FORBIDDEN_KEYWORDS)
def test_forbidden_in_target_escalates_to_l4(kw):
    decision = classify_risk_level({"type": "restart_pod", "target": kw, "params": {}})
    assert decision.risk_level == "L4"
    assert not decision.allowed


@pytest.mark.parametrize("kw", _FORBIDDEN_KEYWORDS)
def test_forbidden_in_params_escalates_to_l4(kw):
    decision = classify_risk_level({"type": "restart_pod", "target": "svc", "params": {"cmd": kw}})
    assert decision.risk_level == "L4"
    assert not decision.allowed


@pytest.mark.parametrize("kw", _FORBIDDEN_KEYWORDS)
def test_forbidden_in_type_escalates_to_l4(kw):
    decision = classify_risk_level({"type": kw, "target": "svc", "params": {}})
    assert decision.risk_level == "L4"
    assert not decision.allowed


def test_forbidden_keyword_case_insensitive():
    decision = classify_risk_level({"type": "restart_pod", "target": "DELETE", "params": {}})
    assert decision.risk_level == "L4"


# "all" must NOT be a forbidden keyword: it is non-destructive on its own and
# appears in legitimate scoping targets/params. Regression for false L4.
@pytest.mark.parametrize(
    "field,value",
    [
        ("target", "all-regions"),
        ("target", "all_tenants"),
        ("target", "rollout-all"),
        ("params", {"scope": "all"}),
    ],
)
def test_all_token_does_not_escalate_to_l4(field, value):
    action = {"type": "scale_deployment", "target": "svc", "params": {}}
    action[field] = value
    decision = classify_risk_level(action)
    assert decision.risk_level != "L4"
    assert decision.allowed


# A genuinely destructive verb combined with "all" still escalates via its verb.
def test_delete_all_still_escalates_to_l4():
    decision = classify_risk_level({"type": "restart_pod", "target": "delete_all", "params": {}})
    assert decision.risk_level == "L4"
    assert not decision.allowed


# ---------------------------------------------------------------------------
# Model escalation via risk_hint
# ---------------------------------------------------------------------------


def test_risk_hint_l3_overrides_default_l2():
    decision = classify_risk_level(
        {"type": "restart_pod", "target": "svc", "params": {}, "risk_hint": "L3"}
    )
    assert decision.risk_level == "L3"
    assert decision.requires_approval


def test_risk_hint_l4_overrides_default_l2():
    decision = classify_risk_level(
        {"type": "restart_pod", "target": "svc", "params": {}, "risk_hint": "L4"}
    )
    assert decision.risk_level == "L4"
    assert not decision.allowed


def test_risk_hint_l3_overrides_l1():
    decision = classify_risk_level(
        {"type": "create_ticket", "target": "svc", "params": {}, "risk_hint": "L3"}
    )
    assert decision.risk_level == "L3"


def test_risk_hint_lower_than_default_is_ignored():
    decision = classify_risk_level(
        {"type": "rollback_release", "target": "svc", "params": {}, "risk_hint": "L0"}
    )
    assert decision.risk_level == "L3"


def test_risk_hint_cannot_downgrade_forbidden_database_session_kill():
    decision = classify_risk_level(
        {
            "type": "kill_idle_transactions",
            "target": "database",
            "params": {"max_idle_seconds": 30},
            "risk_hint": "L2",
        }
    )
    assert decision.risk_level == "L4"
    assert decision.allowed is False
    assert decision.requires_approval is False


def test_risk_hint_case_insensitive():
    decision = classify_risk_level(
        {"type": "restart_pod", "target": "svc", "params": {}, "risk_hint": "l3"}
    )
    assert decision.risk_level == "L3"


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_empty_type_defaults_to_l2():
    decision = classify_risk_level({"type": "", "target": "", "params": {}})
    assert decision.risk_level == "L2"
    assert decision.requires_approval


def test_missing_type_key_defaults_to_l2():
    decision = classify_risk_level({"target": "", "params": {}})
    assert decision.risk_level == "L2"


def test_none_target_handled():
    decision = classify_risk_level({"type": "query_metrics", "target": None, "params": {}})
    assert decision.risk_level == "L0"


def test_empty_params_handled():
    decision = classify_risk_level({"type": "query_metrics", "target": "svc", "params": {}})
    assert decision.risk_level == "L0"


def test_guardrail_decision_fields():
    decision = classify_risk_level({"type": "restart_pod", "target": "svc", "params": {}})
    assert decision.risk_level == "L2"
    assert decision.allowed is True
    assert decision.requires_approval is True
    assert isinstance(decision.reason, str)
    assert len(decision.reason) > 0


def test_guardrail_decision_is_pydantic():
    decision = classify_risk_level({"type": "query_metrics", "target": "", "params": {}})
    assert isinstance(decision, GuardrailDecision)
    d = decision.model_dump()
    assert "risk_level" in d
    assert "allowed" in d
    assert "requires_approval" in d
    assert "reason" in d


def test_l4_not_requires_approval():
    """L4 actions are blocked, not sent to approval."""
    decision = classify_risk_level({"type": "delete_data", "target": "", "params": {}})
    assert decision.risk_level == "L4"
    assert not decision.allowed
    assert not decision.requires_approval


def test_l0_l1_not_requires_approval():
    for atype in ("query_metrics", "query_logs", "create_ticket", "generate_report"):
        decision = classify_risk_level({"type": atype, "target": "", "params": {}})
        assert not decision.requires_approval, f"{atype} should not need approval"


def test_all_known_types_have_reason():
    """Every known action type should return a non-empty reason."""
    for atype, _, _, _ in _RISK_MATRIX:
        decision = classify_risk_level({"type": atype, "target": "svc", "params": {}})
        assert decision.reason, f"reason missing for {atype}"
