"""Deterministic guardrail risk classification. Never trusts LLM output.

The LLM/FakeLLM may propose an action, target, params, and rationale, but this
module is the single source of truth for execution permission. Any change here
can affect live remediation safety, so new action types should be added with
matching tests in ``tests/unit/test_guardrails.py`` and executor/preflight
coverage where applicable.
"""

from __future__ import annotations

import re
from typing import Any

from packages.agent.schemas import GuardrailDecision

# Static risk registry:
#   tuple = (risk_level, requires_approval, human-readable reason)
#
# L0/L1 actions may execute automatically. L2/L3 require approval, with L3
# additionally validated by the approval service's second-confirmation fields.
# L4 is a hard reject and should never enter approval or executor code.
_RISK_TABLE: dict[str, tuple[str, bool, str]] = {
    "query_metrics": ("L0", False, "read-only metrics"),
    "query_logs": ("L0", False, "read-only logs"),
    "query_traces": ("L0", False, "read-only trace"),
    "query_git": ("L0", False, "read-only git"),
    "create_ticket": ("L1", False, "ticket creation"),
    "generate_report": ("L1", False, "report generation"),
    "warmup_cache": ("L1", False, "cache warming"),
    "adjust_connection_pool": ("L1", False, "pool tuning"),
    "restart_pod": ("L2", True, "pod restart needs approval"),
    "restart_deployment": ("L2", True, "deployment restart needs approval"),
    "scale_deployment": ("L2", True, "scaling needs approval"),
    "restart_service": ("L2", True, "service restart needs approval"),
    "pause_rollout": ("L2", True, "rollout pause needs approval"),
    "resume_rollout": ("L2", True, "rollout resume needs approval"),
    "restart_statefulset": ("L2", True, "statefulset restart needs approval"),
    "increase_memory_limit": ("L2", True, "memory limit change needs approval"),
    "enable_rate_limit": ("L3", True, "rate-limit change needs L3"),
    "raise_rate_limit": ("L3", True, "rate-limit change needs L3"),
    "rollback_release": ("L3", True, "rollback needs L3"),
    "rollback_deployment": ("L3", True, "deployment rollback needs L3"),
    "enable_circuit_breaker": ("L3", True, "circuit breaker change needs L3"),
    "switch_dns_resolver": ("L3", True, "DNS routing change needs L3"),
    "failover": ("L3", True, "failover needs L3"),
    "scale_back": ("L2", True, "scale-back after bad scale — needs approval"),
    "revert_config": ("L2", True, "config revert needs approval"),
    "cancel_deployment": ("L3", True, "cancel in-progress deploy needs L3"),
    "kill_idle_transactions": ("L4", False, "database session kill — always rejected"),
    "delete_data": ("L4", False, "destructive — always rejected"),
    "truncate_table": ("L4", False, "destructive — always rejected"),
    "flush_cache": ("L4", False, "destructive — always rejected"),
    "modify_database": ("L4", False, "destructive — always rejected"),
}

_FORBIDDEN = {"delete", "drop", "truncate", "modify_database", "flush"}
# Match forbidden keywords as whole tokens. A naive substring check wrongly
# flags legitimate targets/params: "drop" matches "dropdown"; "delete" matches
# "deleted"; "flush" matches "flushing".
#
# "all" is deliberately NOT forbidden: it is not destructive on its own and
# appears in legitimate targets/params ("all-regions", "all_tenants",
# "rollout-all"). Genuinely destructive variants are already caught by their
# real verb — "delete_all" matches "delete", "drop_all" matches "drop".
#
# Tokens are delimited by any non-alphanumeric char (space, "_", "-", quotes,
# braces). Using alphanumeric lookarounds rather than \b means underscore and
# hyphen act as separators — so "delete_all" still matches "delete" — while
# multi-word keywords like "modify_database" match only when bounded as a unit.
_FORBIDDEN_PATTERN = re.compile(
    r"(?<![a-z0-9])(" + "|".join(re.escape(kw) for kw in _FORBIDDEN) + r")(?![a-z0-9])"
)
# Note: "prod" intentionally excluded — it would block legitimate
# production actions (e.g. restart_pod targeting "checkout-prod").
# Production safety is enforced by L2/L3 approval requirements.
_DEFAULT = ("L2", True, "unknown action — conservative default")


def classify_risk_level(action: dict[str, Any]) -> GuardrailDecision:
    """Return the final deterministic permission decision for one action.

    Decision order matters:
    1. Use the static table, or L2+approval for unknown action types.
    2. Scan type/target/params for destructive vocabulary and escalate to L4.
    3. Honor model ``risk_hint`` only when it increases risk to L3/L4.

    The function never downgrades risk based on model-provided fields.
    """
    action_type = (action.get("type") or "").lower().strip()
    risk_level, needs_approval, reason = _RISK_TABLE.get(action_type, _DEFAULT)

    # Destructive intent can appear in params or target even when the action
    # type looks benign. Scan the combined text after the table lookup so a
    # disguised action such as ``restart_pod`` with target ``drop_table`` still
    # fails closed as L4.
    target = str(action.get("target", "")).lower()
    params_str = str(action.get("params", {})).lower()
    match = _FORBIDDEN_PATTERN.search(f"{action_type} {target} {params_str}")
    if match:
        risk_level, needs_approval, reason = (
            "L4",
            False,
            f"forbidden keyword '{match.group(1)}' — blocked",
        )

    # risk_hint exists so the model can admit uncertainty or danger, not so it
    # can grant itself permission. Only upward L3/L4 escalation is accepted.
    risk_hint = (action.get("risk_hint") or "").upper().strip()
    if risk_hint in ("L3", "L4") and _level(risk_hint) > _level(risk_level):
        risk_level = risk_hint
        needs_approval = risk_level == "L3"
        reason = f"model escalated to {risk_level}"

    return GuardrailDecision(
        risk_level=risk_level,
        allowed=risk_level != "L4",
        requires_approval=needs_approval and risk_level != "L4",
        reason=reason,
    )


def _level(lvl: str) -> int:
    """Convert risk label suffix to an integer for monotonic comparisons."""
    try:
        return int(lvl[-1])
    except (IndexError, ValueError):
        return 0
