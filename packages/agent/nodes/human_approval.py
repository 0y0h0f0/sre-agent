"""Create approval records for L2/L3 actions, then interrupt for human decision."""

from __future__ import annotations

from typing import Any

from langgraph.errors import GraphInterrupt
from langgraph.types import interrupt

from packages.agent.schemas import AgentDeps
from packages.agent.state import IncidentState
from packages.common.ids import new_id
from packages.common.time import utc_now
from packages.db.repositories.actions import ActionRepository
from packages.db.repositories.approvals import ApprovalRepository

# Maximum number of reject -> replan cycles before the run gives up and
# proceeds to report generation. Prevents the rejection path from looping
# forever (the deterministic planner re-proposes the same actions).
MAX_REPLAN_CYCLES = 3


def human_approval(state: IncidentState, deps: AgentDeps) -> IncidentState:
    node_id = new_id("nd_")
    started_at = utc_now()
    actions = state.get("recommended_actions", [])
    approval_actions = [a for a in actions if a.get("requires_approval")]

    if not approval_actions:
        return {**state, "phase": "approval_skipped"}  # type: ignore[typeddict-unknown-key]

    action_repo = ActionRepository(deps.db)
    approval_repo = ApprovalRepository(deps.db)
    approval_status = state.get("approval_status", {})
    raw_approval_ids = (
        approval_status.get("approval_ids", []) if isinstance(approval_status, dict) else []
    )
    approval_ids = [str(item) for item in raw_approval_ids]
    previous_decision = state.get("approval_decision", "")

    try:
        if not previous_decision:
            # First pass: create action + approval records for this batch.
            # Start a fresh approval_ids list so repeated replan cycles do not
            # accumulate stale ids from previously rejected batches.
            approval_ids = []
            for action in approval_actions:
                db_action = action_repo.create(
                    incident_id=state["incident_id"],
                    agent_run_id=state["agent_run_id"],
                    type=action.get("type", "unknown"),
                    risk_level=action.get("risk_level", "L2"),
                    status="waiting_approval",
                    target=action.get("target", ""),
                    params=action.get("params", {}),
                    reason=action.get("reason", ""),
                    rollback_plan=action.get("rollback_plan", ""),
                )
                approval = approval_repo.create(
                    action_id=db_action.action_id,
                    incident_id=state["incident_id"],
                    agent_run_id=state["agent_run_id"],
                )
                approval_ids.append(approval.approval_id)
                action["action_id"] = db_action.action_id

            deps.db.flush()
            state["approval_status"] = {
                "status": "waiting",
                "approval_ids": approval_ids,
            }

            deps.node_tracer(
                node_id=node_id,
                agent_run_id=state["agent_run_id"],
                name="human_approval",
                status="waiting_approval",
                started_at=started_at,
                finished_at=utc_now(),
                input_summary=f"approvals={len(approval_actions)}",
                output_summary=f"ids={approval_ids}",
            )

            if state.get("_interrupts_enabled", True):
                # Pause execution. With a checkpointer this raises GraphInterrupt;
                # the graph resumes by re-running this node with
                # ``approval_decision`` set (see the resume branch below).
                interrupt(
                    {
                        "type": "approval_required",
                        "approval_ids": approval_ids,
                    }
                )

            # No checkpointer (dev/test): auto-approve the whole batch.
            return _auto_approve(state, approval_actions, approval_ids, approval_repo)

        # Resume path: never blanket-apply a single decision to the batch.
        # Read each approval's *actual* status from the DB — the API persists
        # the approve/reject decision before enqueuing the resume — so an
        # approval that a human never reviewed stays out of execution.
        return _apply_db_decisions(state, approval_actions, approval_ids, approval_repo)

    except GraphInterrupt:
        # Must propagate to the graph runtime — don't swallow.
        raise
    except RuntimeError:
        # interrupt() with no checkpointer raises RuntimeError; auto-approve
        # for dev/test convenience.
        deps.node_tracer(
            node_id=node_id,
            agent_run_id=state["agent_run_id"],
            name="human_approval",
            status="succeeded",
            started_at=started_at,
            finished_at=utc_now(),
            input_summary=f"approvals={len(approval_actions)}",
            output_summary="auto-approved (no checkpointer)",
        )
        return _auto_approve(state, approval_actions, approval_ids, approval_repo)
    except Exception as exc:
        deps.node_tracer(
            node_id=node_id,
            agent_run_id=state["agent_run_id"],
            name="human_approval",
            status="failed",
            started_at=started_at,
            finished_at=utc_now(),
            error_message=str(exc),
        )
        state.setdefault("errors", []).append({"node": "human_approval", "error": str(exc)})
        return state


def _auto_approve(
    state: IncidentState,
    approval_actions: list[dict[str, Any]],
    approval_ids: list[str],
    approval_repo: ApprovalRepository,
) -> IncidentState:
    """Approve pending approvals in the batch (dev/test, no human in loop).

    L3 actions (rollback / rate-limit) are NEVER auto-approved: they require an
    explicit human second confirmation. Their approvals stay ``waiting`` and the
    action keeps ``requires_approval`` set, so ``execute_action`` skips them.
    """
    # Map each persisted approval to its action's risk level so L3 can be
    # excluded regardless of approval/action ordering.
    risk_by_action_id = {
        a.get("action_id"): str(a.get("risk_level", "")).upper() for a in approval_actions
    }
    for approval_id in approval_ids:
        existing = approval_repo.get_by_public_id(approval_id)
        if existing is None or existing.status != "waiting":
            continue
        if risk_by_action_id.get(existing.action_id) == "L3":
            # Leave waiting — auto-approval must not bypass L3 second confirmation.
            continue
        approval_repo.update_decision(
            approval_id, status="approved", approver="eval-auto-approver"
        )
    for action in approval_actions:
        if str(action.get("risk_level", "")).upper() == "L3":
            continue
        action["requires_approval"] = False
    return {
        **state,
        "recommended_actions": state.get("recommended_actions", []),
        "approval_status": {"status": "auto_approved", "approval_ids": approval_ids},
        "approval_decision": "",
        "phase": "approval_approved",
    }  # type: ignore[typeddict-unknown-key]


def _apply_db_decisions(
    state: IncidentState,
    approval_actions: list[dict[str, Any]],
    approval_ids: list[str],
    approval_repo: ApprovalRepository,
) -> IncidentState:
    """Reconcile the action batch against the persisted per-approval decisions.

    - approved  -> executable (``requires_approval`` cleared)
    - rejected  -> excluded from execution (``allowed`` set False)
    - waiting   -> left pending; ``execute_action`` skips it (never auto-runs)
    """
    status_by_action: dict[str, str] = {}
    for approval_id in approval_ids:
        approval = approval_repo.get_by_public_id(approval_id)
        if approval is not None:
            status_by_action[approval.action_id] = approval.status

    any_approved = False
    for action in approval_actions:
        decision = status_by_action.get(action.get("action_id", ""), "waiting")
        if decision == "approved":
            action["requires_approval"] = False
            any_approved = True
        elif decision == "rejected":
            action["allowed"] = False
            action["requires_approval"] = False

    actions = state.get("recommended_actions", [])

    if any_approved:
        # At least one approved action — proceed to execute the approved ones.
        # Clear approval_decision so any future cycle re-evaluates cleanly.
        return {
            **state,
            "recommended_actions": actions,
            "approval_status": {"status": "approved", "approval_ids": approval_ids},
            "approval_decision": "",
            "phase": "approval_approved",
        }  # type: ignore[typeddict-unknown-key]

    # Nothing approved -> rejected. Clear approval_decision and reset the
    # approval batch so the replanned actions get a fresh approval round
    # instead of re-entering this node with a stale "rejected" decision
    # (which previously caused an infinite replan loop).
    replan_count = int(state.get("_replan_count", 0)) + 1
    return {
        **state,
        "recommended_actions": actions,
        "approval_status": {"status": "rejected", "approval_ids": approval_ids},
        "approval_decision": "",
        "_replan_count": replan_count,
        "phase": "approval_rejected",
    }  # type: ignore[typeddict-unknown-key]
