"""Classify each action by risk level using deterministic policy."""

from __future__ import annotations

from packages.agent.guardrails.policy import classify_risk_level
from packages.agent.schemas import AgentDeps
from packages.agent.state import IncidentState
from packages.common.ids import new_id
from packages.common.time import utc_now


def guardrail_check(state: IncidentState, deps: AgentDeps) -> IncidentState:
    node_id = new_id("nd_")
    started_at = utc_now()
    try:
        actions = state.get("recommended_actions", [])
        classified = []
        needs_approval = False
        all_l4 = len(actions) > 0  # empty actions → not all L4, route to report

        for action in actions:
            decision = classify_risk_level(action)
            action["risk_level"] = decision.risk_level
            action["allowed"] = decision.allowed
            action["requires_approval"] = decision.requires_approval
            if decision.requires_approval:
                needs_approval = True
            if decision.risk_level != "L4":
                all_l4 = False
            classified.append(action)

        deps.node_tracer(
            node_id=node_id,
            agent_run_id=state["agent_run_id"],
            name="guardrail_check",
            status="succeeded",
            started_at=started_at,
            finished_at=utc_now(),
            input_summary=f"actions={len(actions)}",
            output_summary=f"needs_approval={needs_approval} all_l4={all_l4}",
        )
        return {
            **state,
            "recommended_actions": classified,
            "phase": "guardrail_checked",
            "_needs_approval": needs_approval,
            "_all_l4": all_l4,
        }  # type: ignore[typeddict-unknown-key]
    except Exception as exc:
        deps.node_tracer(
            node_id=node_id,
            agent_run_id=state["agent_run_id"],
            name="guardrail_check",
            status="failed",
            started_at=started_at,
            finished_at=utc_now(),
            error_message=str(exc),
        )
        state.setdefault("errors", []).append({"node": "guardrail_check", "error": str(exc)})
        return state
