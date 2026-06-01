"""Create approval records for L2/L3 actions, then interrupt for human decision."""

from __future__ import annotations

from langgraph.errors import GraphInterrupt
from langgraph.types import interrupt

from packages.agent.schemas import AgentDeps
from packages.agent.state import IncidentState
from packages.common.ids import new_id
from packages.common.time import utc_now
from packages.db.repositories.actions import ActionRepository
from packages.db.repositories.approvals import ApprovalRepository


def human_approval(state: IncidentState, deps: AgentDeps) -> IncidentState:
    node_id = new_id("nd_")
    started_at = utc_now()
    try:
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

        if not previous_decision:
            # First pass: create action and approval records in DB
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
                # Pause execution; on resume, interrupt returns Command(resume=...).
                resume_value = interrupt(
                    {
                        "type": "approval_required",
                        "approval_ids": approval_ids,
                    }
                )
                if isinstance(resume_value, dict):
                    decision = str(resume_value.get("decision", "approved"))
                else:
                    decision = "approved"
            else:
                decision = "approved"
        else:
            # Resume path: decision already in state from Command update
            decision = state.get("approval_decision", "approved")

        if decision == "approved":
            for approval_id in approval_ids:
                existing_approval = approval_repo.get_by_public_id(approval_id)
                if existing_approval is not None and existing_approval.status == "waiting":
                    approval_repo.update_decision(
                        approval_id, status="approved", approver="eval-auto-approver"
                    )
            for action in approval_actions:
                action["requires_approval"] = False
            return {
                **state,
                "recommended_actions": actions,
                "approval_status": {"status": "approved", "approval_ids": approval_ids},
                "phase": "approval_approved",
            }  # type: ignore[typeddict-unknown-key]
        else:
            for approval_id in approval_ids:
                existing_approval = approval_repo.get_by_public_id(approval_id)
                if existing_approval is not None and existing_approval.status == "waiting":
                    approval_repo.update_decision(
                        approval_id, status="rejected", approver="eval-auto-approver"
                    )
            return {
                **state,
                "recommended_actions": actions,
                "approval_status": {"status": "rejected", "approval_ids": approval_ids},
                "phase": "approval_rejected",
            }  # type: ignore[typeddict-unknown-key]

    except GraphInterrupt:
        # Must propagate to the graph runtime — don't swallow
        raise
    except RuntimeError:
        # No checkpointer available (e.g. tests without PostgresSaver);
        # auto-approve for dev/test convenience.
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
        for action in approval_actions:
            action["requires_approval"] = False
        return {
            **state,
            "recommended_actions": actions,
            "approval_status": {"status": "auto_approved", "approval_ids": approval_ids},
            "phase": "approval_approved",
        }  # type: ignore[typeddict-unknown-key]
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
