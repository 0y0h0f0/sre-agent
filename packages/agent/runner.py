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
            "_interrupts_enabled": self.checkpointer is not None,
        }

        try:
            final_state = graph.invoke(initial_state, config)
            if _has_interrupt(final_state):
                return {"status": "waiting_approval", "state": final_state}
            return {"status": "succeeded", "state": final_state}
        except GraphInterrupt:
            # Graph paused at human_approval for L2/L3 actions
            return {"status": "waiting_approval", "state": initial_state}
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
            return {"status": "waiting_approval", "state": {}}
        except Exception as exc:
            return {"status": "failed", "error": str(exc)}


def _has_interrupt(state: Any) -> bool:
    return isinstance(state, dict) and bool(state.get("__interrupt__"))
