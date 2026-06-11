"""Capture pre-action state for rollback and degradation detection.

Runs immediately before execute_action. Stores evidence counts and queries
live backend state (K8s deployment spec) so rollback actions have concrete
parameters rather than relying on LLM memory.
"""

from __future__ import annotations

import logging
from typing import Any

from packages.agent.schemas import AgentDeps
from packages.agent.state import IncidentState
from packages.common.ids import new_id
from packages.common.time import utc_now
from packages.tools.executor_backends import ROLLBACK_ACTION_TYPES
from packages.tools.k8s import K8sQuery

logger = logging.getLogger(__name__)


def take_snapshot(state: IncidentState, deps: AgentDeps) -> IncidentState:
    """Freeze pre-execution evidence counts and live state for rollback reference.

    The snapshot is used downstream by:
    - ``verify``: degraded detection compares against snapshot baselines.
    - ``plan_actions``: when degraded, the LLM receives concrete values
      (revision, replicas, image) from the snapshot instead of guessing.
    """
    node_id = new_id("nd_")
    started_at = utc_now()
    try:
        actions = state.get("recommended_actions", [])
        existing_snapshot = state.get("pre_action_snapshot", {})
        if (
            state.get("verify_result") == "degraded"
            and _has_rollback_action(actions)
            and _usable_snapshot(existing_snapshot)
        ):
            deps.node_tracer(
                node_id=node_id,
                agent_run_id=state["agent_run_id"],
                name="take_snapshot",
                status="succeeded",
                started_at=started_at,
                finished_at=utc_now(),
                input_summary=f"actions={len(actions)} degraded=true",
                output_summary="preserved rollback reference snapshot",
            )
            return {
                **state,
                "pre_action_snapshot": existing_snapshot,
                "phase": "snapshot_preserved",
            }  # type: ignore[typeddict-unknown-key]

        snapshot: dict[str, Any] = {
            "taken_at": utc_now().isoformat(),
            "action_types": [a.get("type") for a in actions],
            "evidence_counts": {
                "metrics": len(state.get("metrics_evidence", [])),
                "logs": len(state.get("logs_evidence", [])),
                "traces": len(state.get("traces_evidence", [])),
            },
        }

        # If K8s actions are pending, capture current deployment spec so
        # rollback can use concrete revision/replica values from snapshot.
        k8s_action_types = {
            "restart_pod", "restart_service", "scale_deployment",
            "rollback_release", "scale_back",
        }
        k8s_actions = [
            a for a in actions
            if str(a.get("type", "")).lower() in k8s_action_types
        ]
        if k8s_actions and deps.k8s_tool:
            try:
                svc = state.get("service_name", "unknown")
                namespace = deps.settings.executor_k8s_namespace or "default"
                result = deps.k8s_tool.run(
                    K8sQuery(
                        service=svc,
                        operation="get_deployment",
                        namespace=namespace,
                    )
                )
                snapshot["k8s"] = (
                    result.data.get("payload", {}) if result.data else {}
                )
            except Exception:
                logger.error(
                    "take_snapshot: k8s query failed service=%s",
                    svc, exc_info=True,
                )
                snapshot["k8s"] = {"error": "k8s_unreachable"}

        deps.node_tracer(
            node_id=node_id,
            agent_run_id=state["agent_run_id"],
            name="take_snapshot",
            status="succeeded",
            started_at=started_at,
            finished_at=utc_now(),
            input_summary=f"actions={len(actions)}",
            output_summary=f"snapshot_keys={list(snapshot.keys())}",
        )
        return {
            **state,
            "pre_action_snapshot": snapshot,
            "phase": "snapshot_taken",
        }  # type: ignore[typeddict-unknown-key]

    except Exception as exc:
        logger.error(
            "take_snapshot: node failed incident=%s",
            state.get("incident_id"), exc_info=True,
        )
        deps.node_tracer(
            node_id=node_id,
            agent_run_id=state["agent_run_id"],
            name="take_snapshot",
            status="failed",
            started_at=started_at,
            finished_at=utc_now(),
            error_message=str(exc),
        )
        errors = list(state.get("errors", []))
        errors.append({"node": "take_snapshot", "error": str(exc)})
        # Snapshot failure is non-fatal — degrade gracefully.
        return {
            **state,
            "pre_action_snapshot": {"error": str(exc)},
            "phase": "snapshot_failed",
            "errors": errors,
        }  # type: ignore[typeddict-unknown-key]


def _has_rollback_action(actions: list[dict[str, Any]]) -> bool:
    return any(
        str(action.get("type", "")).lower() in ROLLBACK_ACTION_TYPES
        for action in actions
    )


def _usable_snapshot(snapshot: object) -> bool:
    if not isinstance(snapshot, dict) or not snapshot:
        return False
    if "error" in snapshot:
        return False
    # Also reject snapshots with nested K8s errors.
    k8s = snapshot.get("k8s")
    if isinstance(k8s, dict) and "error" in k8s:
        return False
    return True
