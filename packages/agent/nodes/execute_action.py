"""Mock action executor — deterministic, no real system calls."""

from __future__ import annotations

from packages.agent.schemas import AgentDeps
from packages.agent.state import IncidentState
from packages.common.ids import new_id
from packages.common.time import utc_now
from packages.db.repositories.actions import ActionRepository
from packages.tools.mock_executor import MOCK_EXECUTOR_RESULTS


def execute_action(state: IncidentState, deps: AgentDeps) -> IncidentState:
    node_id = new_id("nd_")
    started_at = utc_now()
    try:
        actions = state.get("recommended_actions", [])
        executable = [a for a in actions if a.get("allowed") and not a.get("requires_approval")]
        action_repo = ActionRepository(deps.db)
        results = []

        for action in executable:
            atype = action.get("type", "unknown")
            fallback = {"status": "succeeded", "message": f"mock {atype} completed"}
            mock_result = MOCK_EXECUTOR_RESULTS.get(atype, fallback)
            aid = action.get("action_id", "")
            if aid:
                action_repo.update_status(aid, "succeeded", mock_result)
            results.append({**action, "execution_result": mock_result})

        deps.node_tracer(
            node_id=node_id,
            agent_run_id=state["agent_run_id"],
            name="execute_action",
            status="succeeded",
            started_at=started_at,
            finished_at=utc_now(),
            input_summary=f"executable={len(executable)}",
            output_summary=f"executed={len(results)}",
        )
        return {**state, "execution_results": results, "phase": "actions_executed"}
    except Exception as exc:
        deps.node_tracer(
            node_id=node_id,
            agent_run_id=state["agent_run_id"],
            name="execute_action",
            status="failed",
            started_at=started_at,
            finished_at=utc_now(),
            error_message=str(exc),
        )
        state.setdefault("errors", []).append({"node": "execute_action", "error": str(exc)})
        return state
