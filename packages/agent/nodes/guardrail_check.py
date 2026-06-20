"""Classify each action by risk level using deterministic policy."""

from __future__ import annotations

from packages.agent.guardrails.policy import classify_risk_level
from packages.agent.schemas import AgentDeps
from packages.agent.state import IncidentState
from packages.common.ids import new_id
from packages.common.time import utc_now


def guardrail_check(state: IncidentState, deps: AgentDeps) -> IncidentState:
    """Attach deterministic risk metadata to every planned action.

    The planner output is treated as untrusted input. This node does not execute
    anything; it annotates actions with ``risk_level``, ``allowed``, and
    ``requires_approval`` so graph routing and executor filtering can make the
    same safety decision from persisted state.
    """
    node_id = new_id("nd_")
    started_at = utc_now()
    try:
        actions = state.get("recommended_actions", [])
        classified = []
        needs_approval = False
        # Empty action lists should not be considered "all L4"; they route to
        # execute, where no executable actions are found, then verify/report.
        all_l4 = len(actions) > 0  # empty actions → not all L4, route to report

        for action in actions:
            # Mutating the action dict is intentional: downstream nodes preserve
            # the action payload but rely on guardrail-owned fields for safety.
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
        errors = list(state.get("errors", []))
        errors.append({"node": "guardrail_check", "error": str(exc)})
        return {**state, "errors": errors}
