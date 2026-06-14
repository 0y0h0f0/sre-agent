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
        executable = [a for a in actions if a.get("allowed") and not a.get("requires_approval")]
        action_repo = ActionRepository(deps.db)
        backend = deps.executor_backend
        if backend is None:
            backend = FixtureExecutorBackend()
        _persist_missing_executable_actions(
            executable,
            state=state,
            action_repo=action_repo,
            executor=getattr(backend, "name", "unknown"),
        )

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
                if state.get("verify_result") == "degraded" and atype in ROLLBACK_ACTION_TYPES:
                    result = backend.rollback(action, state.get("pre_action_snapshot", {}), context)
                else:
                    # Reject non-rollback actions when degraded.
                    if state.get("verify_result") == "degraded":
                        result = ExecutionResult(
                            status="failed",
                            message=(f"non-rollback action '{atype}' rejected after degradation"),
                        )
                    else:
                        result = backend.execute(action, context)
                failed += 1 if result.status == "failed" else 0
            except Exception as exc:
                failed += 1
                logger.error(
                    "execute_action: action=%s target=%s failed",
                    atype,
                    action.get("target", ""),
                    exc_info=True,
                )
                result = ExecutionResult(
                    status="failed",
                    message=f"action '{atype}' raised exception",
                    details={"error_type": type(exc).__name__},
                )

            # Persist action status (best-effort; failure here must not
            # discard prior results).
            aid = action.get("action_id", "")
            if aid:
                try:
                    action_repo.update_status(aid, result.status, result.model_dump())
                except Exception:
                    logger.error(
                        "execute_action: db update_status failed for action=%s",
                        aid,
                        exc_info=True,
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
            state.get("incident_id"),
            state.get("agent_run_id"),
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


def _persist_missing_executable_actions(
    actions: list[dict[str, Any]],
    *,
    state: IncidentState,
    action_repo: ActionRepository,
    executor: str,
) -> None:
    """Create Action rows for automatic actions before any executor call.

    L2/L3 approval paths already create Action rows in ``human_approval``.
    L0/L1 actions bypass that node, so they must be persisted here to keep the
    incident/action APIs and reports aligned with what the graph executes.
    """
    current_run_action_ids = _validate_existing_action_ids(
        actions, state=state, action_repo=action_repo
    )

    created = False
    for action in actions:
        existing_id = str(action.get("action_id", ""))
        if existing_id in current_run_action_ids:
            continue
        action.pop("action_id", None)
        db_action = action_repo.create(
            incident_id=state["incident_id"],
            agent_run_id=state["agent_run_id"],
            type=action.get("type", "unknown"),
            risk_level=action.get("risk_level", "L1"),
            status="executing",
            executor=executor,
            target=action.get("target", ""),
            params=action.get("params", {}),
            reason=action.get("reason", ""),
            rollback_plan=action.get("rollback_plan", ""),
        )
        action["action_id"] = db_action.action_id
        created = True

    if created:
        action_repo.db.flush()


def _validate_existing_action_ids(
    actions: list[dict[str, Any]],
    *,
    state: IncidentState,
    action_repo: ActionRepository,
) -> set[str]:
    """Fail closed before creating rows if an approval-gated ID is stale."""
    current_run_action_ids: set[str] = set()
    for action in actions:
        existing_id = str(action.get("action_id", ""))
        if not existing_id:
            continue
        if _action_id_belongs_to_current_run(
            existing_id,
            state=state,
            action_repo=action_repo,
        ):
            current_run_action_ids.add(existing_id)
            continue
        if _requires_approval_level(action):
            raise RuntimeError(
                f"action_id {existing_id} does not belong to current run; "
                "refusing to execute approval-gated action"
            )
    return current_run_action_ids


def _action_id_belongs_to_current_run(
    action_id: str,
    *,
    state: IncidentState,
    action_repo: ActionRepository,
) -> bool:
    db_action = action_repo.get_by_public_id(action_id)
    return (
        db_action is not None
        and db_action.incident_id == state["incident_id"]
        and db_action.agent_run_id == state["agent_run_id"]
    )


def _requires_approval_level(action: dict[str, Any]) -> bool:
    return str(action.get("risk_level", "")).upper() in {"L2", "L3"}
