"""Action executor — delegates to the pluggable ExecutorBackend.

When ``executor_backend`` is None (no executor injected), falls back to the
fixture backend so existing tests and dev setups continue to work unchanged.
"""

from __future__ import annotations

import logging
from typing import Any

from packages.agent.actions.capabilities import (
    ActionCapability,
    get_action_capability,
)
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
    canonical_action_type,
    has_live_rollback_handler,
    is_valid_k8s_resource_name,
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
            atype = canonical_action_type(action.get("type"))
            _attach_capability_metadata(action, atype)
            try:
                preflight_block = _live_preflight_block(action, state, context, backend)
                if preflight_block is not None:
                    result = preflight_block
                elif state.get("verify_result") == "degraded" and atype in ROLLBACK_ACTION_TYPES:
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
                failed += 1 if result.status in {"failed", "blocked", "timeout"} else 0
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


def _attach_capability_metadata(action: dict[str, Any], action_type: str) -> None:
    capability = get_action_capability(action_type)
    if capability is None:
        return
    action["capability"] = {
        "category": capability.category,
        "live_backend": capability.live_backend,
        "reversible": capability.reversible,
        "bounded_irreversible": capability.bounded_irreversible,
        "verify_gates": list(capability.verify_gates),
    }


def _live_preflight_block(
    action: dict[str, Any],
    state: IncidentState,
    context: ExecutionContext,
    backend: Any,
) -> ExecutionResult | None:
    """Return a blocked result when a live action fails capability preflight."""
    if getattr(backend, "name", "") != "live":
        return None

    atype = canonical_action_type(action.get("type"))
    capability = get_action_capability(atype)
    if capability is None:
        return _blocked_result(
            atype,
            "unknown live action capability",
            {"action_type": atype},
        )

    if capability.category in {"read_only", "record_only"}:
        return None

    if capability.live_backend != "k8s":
        return _blocked_result(
            atype,
            "action is not registered for live K8s execution",
            {"category": capability.category, "live_backend": capability.live_backend},
        )

    contract_error = _capability_contract_error(capability)
    if contract_error:
        return _blocked_result(atype, contract_error, {"capability": capability.model_dump()})

    common_errors = _common_k8s_preflight_errors(action, context)
    if common_errors:
        return _blocked_result(atype, "live K8s target preflight failed", {"failed": common_errors})

    snapshot = _snapshot_for_action(state.get("pre_action_snapshot", {}), action)
    missing_paths = _missing_snapshot_paths(snapshot, capability.required_snapshot_paths)
    if missing_paths:
        return _blocked_result(
            atype,
            "required pre-action snapshot fields are missing",
            {"missing_snapshot_paths": missing_paths},
        )

    identity_errors = _snapshot_identity_errors(snapshot, action, context)
    if identity_errors:
        return _blocked_result(
            atype,
            "pre-action snapshot does not match live K8s target",
            {"failed": identity_errors},
        )

    failed_checks = _failed_preflight_checks(capability, action, snapshot, context)
    if failed_checks:
        return _blocked_result(
            atype,
            "live capability preflight checks failed",
            {"failed_preflight_checks": failed_checks},
        )

    return None


def _snapshot_for_action(snapshot: object, action: dict[str, Any]) -> object:
    if not isinstance(snapshot, dict):
        return snapshot
    k8s_targets = snapshot.get("k8s_targets")
    target = str(action.get("target", "") or "")
    if isinstance(k8s_targets, dict) and target:
        target_snapshot = k8s_targets.get(target)
        if isinstance(target_snapshot, dict):
            return {**snapshot, "k8s": target_snapshot}
    return snapshot


def _capability_contract_error(capability: ActionCapability) -> str:
    if not (capability.reversible or capability.bounded_irreversible):
        return "live mutation is neither reversible nor bounded irreversible"
    if not capability.verify_gates:
        return "live mutation has no verify gates"
    if capability.reversible:
        if not capability.rollback_action_type:
            return "reversible live mutation has no rollback action"
        rollback_capability = get_action_capability(capability.rollback_action_type)
        if rollback_capability is None:
            return "rollback action is not registered"
        if not has_live_rollback_handler(capability.rollback_action_type):
            return "rollback action has no live rollback handler"
    if capability.bounded_irreversible and not capability.preflight_checks:
        return "bounded irreversible live mutation has no preflight checks"
    return ""


def _common_k8s_preflight_errors(
    action: dict[str, Any],
    context: ExecutionContext,
) -> list[str]:
    errors: list[str] = []
    target = str(action.get("target", "") or "")
    namespace = context.namespace or ""
    if not target or not is_valid_k8s_resource_name(target):
        errors.append("k8s_target_name_valid")
    if namespace and not is_valid_k8s_resource_name(namespace):
        errors.append("k8s_namespace_valid")
    return errors


def _missing_snapshot_paths(snapshot: object, paths: tuple[str, ...]) -> list[str]:
    return [path for path in paths if not _snapshot_path_exists(snapshot, path)]


def _snapshot_path_exists(snapshot: object, path: str) -> bool:
    current: object = snapshot
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return False
        current = current[part]
    return current is not None and current != "" and current != []


def _failed_preflight_checks(
    capability: ActionCapability,
    action: dict[str, Any],
    snapshot: object,
    context: ExecutionContext,
) -> list[str]:
    failed: list[str] = []
    k8s_snapshot = snapshot.get("k8s") if isinstance(snapshot, dict) else None
    if not isinstance(k8s_snapshot, dict):
        k8s_snapshot = {}

    for check in capability.preflight_checks:
        if check == "k8s_target_name_valid":
            target_errors = _common_k8s_preflight_errors(action, context)
            if "k8s_target_name_valid" in target_errors:
                failed.append(check)
        elif check == "k8s_namespace_valid":
            target_errors = _common_k8s_preflight_errors(action, context)
            if "k8s_namespace_valid" in target_errors:
                failed.append(check)
        elif check == "k8s_deployment_exists":
            if k8s_snapshot.get("error") or not k8s_snapshot.get("name"):
                failed.append(check)
        elif check == "k8s_replicas_gt_zero":
            try:
                if int(k8s_snapshot.get("replicas", 0)) <= 0:
                    failed.append(check)
            except (TypeError, ValueError):
                failed.append(check)
        elif check == "k8s_rollout_not_failed":
            if _rollout_failed(k8s_snapshot):
                failed.append(check)
        elif check == "k8s_rolling_restart_patch_only":
            if canonical_action_type(action.get("type")) not in {"restart_pod", "restart_service"}:
                failed.append(check)
        elif check == "k8s_rollout_pause_patch_only":
            if canonical_action_type(action.get("type")) != "pause_rollout":
                failed.append(check)
        else:
            failed.append(check)
    return failed


def _snapshot_identity_errors(
    snapshot: object,
    action: dict[str, Any],
    context: ExecutionContext,
) -> list[str]:
    k8s_snapshot = snapshot.get("k8s") if isinstance(snapshot, dict) else None
    if not isinstance(k8s_snapshot, dict):
        return []

    failed: list[str] = []
    expected_target = str(action.get("target", "") or "")
    snapshot_name = str(k8s_snapshot.get("name", "") or "")
    if expected_target and snapshot_name and snapshot_name != expected_target:
        failed.append("k8s_snapshot_target_matches_action")

    expected_namespace = context.namespace or ""
    snapshot_namespace = str(k8s_snapshot.get("namespace", "") or "")
    if expected_namespace and snapshot_namespace and snapshot_namespace != expected_namespace:
        failed.append("k8s_snapshot_namespace_matches_context")
    return failed


def _rollout_failed(k8s_snapshot: dict[str, Any]) -> bool:
    for condition in k8s_snapshot.get("conditions", []) or []:
        if not isinstance(condition, dict):
            continue
        ctype = str(condition.get("type", ""))
        status = str(condition.get("status", "")).lower()
        if ctype == "Progressing" and status == "false":
            return True
        if ctype == "ReplicaFailure" and status == "true":
            return True
    return False


def _blocked_result(
    action_type: str,
    reason: str,
    details: dict[str, Any],
) -> ExecutionResult:
    return ExecutionResult(
        status="blocked",
        message=f"live action '{action_type}' blocked: {reason}",
        details={"reason": reason, **details},
    )


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
