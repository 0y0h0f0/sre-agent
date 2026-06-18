"""Capture pre-action state for rollback and degradation detection.

Runs immediately before execute_action. Stores evidence counts and queries
live backend state (K8s deployment spec) so rollback actions have concrete
parameters rather than relying on LLM memory.
"""

from __future__ import annotations

import logging
from typing import Any

from packages.agent.nodes._k8s_targeting import effective_executor_k8s_namespace
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
            "restart_pod",
            "restart_deployment",
            "restart_service",
            "restart_statefulset",
            "pause_rollout",
            "resume_rollout",
            "scale_deployment",
            "rollback_release",
            "rollback_deployment",
            "scale_back",
        }
        k8s_actions = [a for a in actions if str(a.get("type", "")).lower() in k8s_action_types]
        if k8s_actions and deps.k8s_tool:
            k8s_targets: dict[str, Any] = {}
            ordered_targets = _k8s_snapshot_targets(k8s_actions, state)
            namespace = effective_executor_k8s_namespace(deps.settings)
            try:
                for target in ordered_targets:
                    operation = _k8s_snapshot_operation(k8s_actions, target)
                    result = deps.k8s_tool.run(
                        K8sQuery(
                            service=target,
                            operation=operation,
                            namespace=namespace,
                        )
                    )
                    k8s_targets[target] = result.data.get("payload", {}) if result.data else {}
                snapshot["k8s_targets"] = k8s_targets
                if ordered_targets:
                    snapshot["k8s"] = k8s_targets.get(ordered_targets[0], {})
            except Exception:
                logger.error(
                    "take_snapshot: k8s query failed targets=%s",
                    ordered_targets,
                    exc_info=True,
                )
                snapshot["k8s"] = {"error": "k8s_unreachable"}
                snapshot["k8s_targets"] = {
                    target: {"error": "k8s_unreachable"} for target in ordered_targets
                }

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
            state.get("incident_id"),
            exc_info=True,
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
    return any(str(action.get("type", "")).lower() in ROLLBACK_ACTION_TYPES for action in actions)


def _k8s_snapshot_targets(
    actions: list[dict[str, Any]],
    state: IncidentState,
) -> list[str]:
    """Return unique Deployment targets to snapshot, preserving action order."""
    fallback = str(state.get("service_name", "") or "unknown")
    targets: list[str] = []
    seen: set[str] = set()
    for action in actions:
        target = str(action.get("target") or fallback)
        if target in seen:
            continue
        targets.append(target)
        seen.add(target)
    return targets


def _k8s_snapshot_operation(actions: list[dict[str, Any]], target: str) -> str:
    for action in actions:
        action_target = str(action.get("target") or target)
        if action_target != target:
            continue
        if str(action.get("type", "")).lower() == "restart_statefulset":
            return "get_statefulset"
        return "get_deployment"
    return "get_deployment"


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
