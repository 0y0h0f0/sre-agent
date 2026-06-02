"""Generate remediation actions via LLM with deterministic fallback."""

from __future__ import annotations

from packages.agent.llm.reasoning import (
    capture_metadata,
    format_call_metadata,
    record_llm_call,
    should_use_deep_reasoning,
)
from packages.agent.schemas import AgentDeps, PlannedAction
from packages.agent.state import IncidentState
from packages.common.ids import new_id
from packages.common.time import utc_now

_NODE_NAME = "plan_actions"


def plan_actions(state: IncidentState, deps: AgentDeps) -> IncidentState:
    node_id = new_id("nd_")
    started_at = utc_now()
    try:
        root_cause = state.get("root_cause", {})
        prompt = f"Plan actions for {state.get('alert_name', '')}: {root_cause.get('summary', '')}"
        thinking = should_use_deep_reasoning(deps.settings, _NODE_NAME)

        meta: dict[str, object] = {}
        try:
            models = deps.llm.generate_json(prompt, list[PlannedAction], thinking=thinking)
            actions = [a.model_dump() for a in models]
            meta = capture_metadata(deps.llm)
        except Exception:
            from packages.agent.fake_llm import _ACTIONS_MAP

            fallback = _ACTIONS_MAP.get(
                state.get("alert_name", ""), _ACTIONS_MAP["High5xxAfterDeploy"]
            )
            actions = fallback

        record_llm_call(state, _NODE_NAME, meta)
        meta_summary = format_call_metadata(meta)

        deps.node_tracer(
            node_id=node_id,
            agent_run_id=state["agent_run_id"],
            name="plan_actions",
            status="succeeded",
            started_at=started_at,
            finished_at=utc_now(),
            input_summary=root_cause.get("summary", "")[:80],
            output_summary=f"proposed {len(actions)} actions {meta_summary}".strip(),
        )
        return {**state, "recommended_actions": actions, "phase": "actions_planned"}
    except Exception as exc:
        deps.node_tracer(
            node_id=node_id,
            agent_run_id=state["agent_run_id"],
            name="plan_actions",
            status="failed",
            started_at=started_at,
            finished_at=utc_now(),
            error_message=str(exc),
        )
        state.setdefault("errors", []).append({"node": "plan_actions", "error": str(exc)})
        return state
