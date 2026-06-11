"""Action executor — delegates to the pluggable ExecutorBackend.

When ``executor_backend`` is None (no executor injected), falls back to the
fixture backend so existing tests and dev setups continue to work unchanged.
"""

from __future__ import annotations

import logging
from typing import Any

from packages.agent.schemas import AgentDeps
from packages.agent.state import IncidentState
from packages.common.ids import new_id
from packages.common.time import utc_now
from packages.db.repositories.actions import ActionRepository
from packages.tools.executor_backends import (
    ROLLBACK_ACTION_TYPES,
    ExecutionContext,
    ExecutionResult,
    FixtureExecutorBackend,
)

logger = logging.getLogger(__name__)


def execute_action(state: IncidentState, deps: AgentDeps) -> IncidentState:
    node_id = new_id("nd_")
    started_at = utc_now()
    try:
        actions = state.get("recommended_actions", [])
        executable = [
            a for a in actions
            if a.get("allowed") and not a.get("requires_approval")
        ]
        action_repo = ActionRepository(deps.db)
        backend = deps.executor_backend
        if backend is None:
            backend = FixtureExecutorBackend()

        context = ExecutionContext(
            service=state.get("service_name", "unknown"),
            incident_id=state["incident_id"],
            agent_run_id=state["agent_run_id"],
            namespace=deps.settings.executor_k8s_namespace or None,
        )
        results: list[dict[str, Any]] = []
        failed = 0

        for action in executable:
            atype = str(action.get("type", "")).lower()
            try:
                if (
                    state.get("verify_result") == "degraded"
                    and atype in ROLLBACK_ACTION_TYPES
                ):
                    result = backend.rollback(
                        action, state.get("pre_action_snapshot", {}), context
                    )
                else:
                    # Reject non-rollback actions when degraded.
                    if state.get("verify_result") == "degraded":
                        result = ExecutionResult(
                            status="failed",
                            message=(
                                f"non-rollback action '{atype}' rejected "
                                "after degradation"
                            ),
                        )
                    else:
                        result = backend.execute(action, context)
                failed += 1 if result.status == "failed" else 0
            except Exception as exc:
                failed += 1
                logger.error(
                    "execute_action: action=%s target=%s failed",
                    atype, action.get("target", ""), exc_info=True,
                )
                result = ExecutionResult(
                    status="failed",
                    message=f"action '{atype}' raised exception",
                    details={"error_type": type(exc).__name__},
                )
                failed += 1

            # Persist action status (best-effort; failure here must not
            # discard prior results).
            aid = action.get("action_id", "")
            if aid:
                try:
                    action_repo.update_status(
                        aid, result.status, result.model_dump()
                    )
                except Exception:
                    logger.error(
                        "execute_action: db update_status failed for action=%s",
                        aid, exc_info=True,
                    )
            results.append({**action, "execution_result": result.model_dump()})

        deps.node_tracer(
            node_id=node_id,
            agent_run_id=state["agent_run_id"],
            name="execute_action",
            status="succeeded" if failed == 0 else "degraded",
            started_at=started_at,
            finished_at=utc_now(),
            input_summary=f"executable={len(executable)}",
            output_summary=f"executed={len(results)} failed={failed}",
        )
        return {
            **state,
            "execution_results": results,
            "phase": "actions_executed",
        }  # type: ignore[typeddict-unknown-key]
    except Exception as exc:
        logger.error(
            "execute_action: node failed incident=%s run=%s",
            state.get("incident_id"), state.get("agent_run_id"),
            exc_info=True,
        )
        deps.node_tracer(
            node_id=node_id,
            agent_run_id=state["agent_run_id"],
            name="execute_action",
            status="failed",
            started_at=started_at,
            finished_at=utc_now(),
            error_message=str(exc),
        )
        errors = list(state.get("errors", []))
        errors.append({"node": "execute_action", "error": str(exc)})
        return {**state, "errors": errors}
