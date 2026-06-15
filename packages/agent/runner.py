"""AgentRunner — executes the LangGraph workflow with checkpointing and interrupts."""

from __future__ import annotations

from typing import Any

from langgraph.errors import GraphInterrupt
from langgraph.types import Command

from packages.agent.graph import build_graph
from packages.agent.schemas import AgentDeps
from packages.agent.state import IncidentState


class AgentRunner:
    """Executes the LangGraph diagnosis workflow.

    Handles graph construction, checkpoint config, interrupt for
    human-in-the-loop approval, and resume after decision.
    """

    def __init__(self, deps: AgentDeps, checkpointer: Any | None = None) -> None:
        self.deps = deps
        self.checkpointer = checkpointer

    def run(
        self, incident_id: str, agent_run_id: str, alert_payload: dict[str, Any]
    ) -> dict[str, Any]:
        """Execute the diagnosis workflow. Returns ``waiting_approval`` on interrupt."""
        graph = build_graph(self.deps, self.checkpointer)
        config: dict[str, Any] = {"configurable": {"thread_id": agent_run_id, "checkpoint_ns": ""}}

        initial_state: IncidentState = {
            "incident_id": incident_id,
            "agent_run_id": agent_run_id,
            "alert_payload": alert_payload,
            "metrics_evidence": [],
            "logs_evidence": [],
            "traces_evidence": [],
            "deployment_evidence": [],
            "runbook_context": [],
            "memory_context": [],
            "hypotheses": [],
            "root_cause": {},
            "recommended_actions": [],
            "approval_status": {},
            "execution_results": [],
            "incident_report": {},
            "token_budget": {},
            "compression_events": [],
            "errors": [],
            "phase": "initial",
            "_needs_approval": False,
            "_all_l4": False,
            "approval_decision": "",
            "rejection_feedback": "",
            "_replan_count": 0,
            "verify_result": "",
            "verify_evidence": [],
            "verify_gates": [],
            "_verify_cycles": 0,
            "_collect_gap_cycles": 0,
            "pre_action_snapshot": {},
            # rollback_count removed (dead field)
            "_interrupts_enabled": self.checkpointer is not None,
        }

        try:
            final_state = graph.invoke(initial_state, config)
            if _has_interrupt(final_state):
                return {"status": "waiting_approval", "state": final_state}
            return {"status": "succeeded", "state": final_state}
        except GraphInterrupt:
            # Graph paused at human_approval for L2/L3 actions.
            # Fetch checkpoint state so the caller gets the actual diagnosis
            # context (hypotheses, root cause, evidence) rather than the
            # empty initial state.
            return {
                "status": "waiting_approval",
                "state": _checkpoint_state(graph, config, dict(initial_state)),
            }
        except Exception as exc:
            return {"status": "failed", "error": str(exc), "state": initial_state}

    def resume(self, agent_run_id: str, decision: str) -> dict[str, Any]:
        """Resume a graph interrupted at human_approval.

        Passes the approval decision via ``Command(resume=...)`` so
        ``interrupt()`` inside the node returns the decision dict.
        """
        graph = build_graph(self.deps, self.checkpointer)
        config: dict[str, Any] = {"configurable": {"thread_id": agent_run_id, "checkpoint_ns": ""}}
        try:
            final_state = graph.invoke(
                Command(
                    resume={"decision": decision},
                    update={"approval_decision": decision},
                ),
                config,
            )
            if _has_interrupt(final_state):
                return {"status": "waiting_approval", "state": final_state}
            return {"status": "succeeded", "state": final_state}
        except GraphInterrupt:
            # Re-interrupted — fetch checkpoint state so callers get updated
            # diagnosis context rather than an empty dict.
            return {"status": "waiting_approval", "state": _checkpoint_state(graph, config, {})}
        except Exception as exc:
            return {"status": "failed", "error": str(exc)}


def _has_interrupt(state: Any) -> bool:
    return isinstance(state, dict) and bool(state.get("__interrupt__"))


def _checkpoint_state(
    graph: Any, config: dict[str, Any], fallback: dict[str, Any]
) -> dict[str, Any]:
    """Retrieve the latest checkpoint state from the graph.

    Used after a ``GraphInterrupt`` to return the actual diagnostic state
    (hypotheses, evidence, root cause) instead of the empty initial state.
    Falls back to ``fallback`` when checkpoint retrieval fails.
    """
    try:
        latest = graph.get_state(config)
        if latest and latest.values:
            sanitized: dict[str, Any] = {}
            for k, v in latest.values.items():
                if not k.startswith("__"):
                    sanitized[k] = v
            if sanitized:
                return sanitized
    except Exception:
        import logging

        logging.getLogger(__name__).warning(
            "Failed to retrieve checkpoint state for run %s — email notifications may be skipped",
            config.get("configurable", {}).get("thread_id", "unknown"),
            exc_info=True,
        )
    return dict(fallback)
