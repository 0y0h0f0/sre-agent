"""Deterministic guardrail risk classification. Never trusts LLM output."""

from __future__ import annotations

from typing import Any

from packages.agent.schemas import GuardrailDecision

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
    "scale_deployment": ("L2", True, "scaling needs approval"),
    "restart_service": ("L2", True, "service restart needs approval"),
    "enable_rate_limit": ("L3", True, "rate-limit change needs L3"),
    "rollback_release": ("L3", True, "rollback needs L3"),
    "delete_data": ("L4", False, "destructive — always rejected"),
    "truncate_table": ("L4", False, "destructive — always rejected"),
    "flush_cache": ("L4", False, "destructive — always rejected"),
    "modify_database": ("L4", False, "destructive — always rejected"),
}

_FORBIDDEN = {"delete", "drop", "truncate", "modify_database", "flush", "all"}
# Note: "prod" intentionally excluded — it would block legitimate
# production actions (e.g. restart_pod targeting "checkout-prod").
# Production safety is enforced by L2/L3 approval requirements.
_DEFAULT = ("L2", True, "unknown action — conservative default")


def classify_risk_level(action: dict[str, Any]) -> GuardrailDecision:
    action_type = (action.get("type") or "").lower().strip()
    risk_level, needs_approval, reason = _RISK_TABLE.get(action_type, _DEFAULT)

    target = str(action.get("target", "")).lower()
    params_str = str(action.get("params", {})).lower()
    for kw in _FORBIDDEN:
        if kw in f"{action_type} {target} {params_str}":
            risk_level, needs_approval, reason = "L4", False, f"forbidden keyword '{kw}' — blocked"
            break

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
    try:
        return int(lvl[-1])
    except (IndexError, ValueError):
        return 0
