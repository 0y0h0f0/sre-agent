"""Retrieve relevant runbook chunks via RunbookSearchTool."""

from __future__ import annotations

from packages.agent.schemas import AgentDeps
from packages.agent.state import IncidentState
from packages.common.ids import new_id
from packages.common.time import utc_now
from packages.tools.runbook_search import RunbookSearchQuery


def retrieve_runbook(state: IncidentState, deps: AgentDeps) -> IncidentState:
    node_id = new_id("nd_")
    started_at = utc_now()
    try:
        service = state["service_name"]
        alert_name = state["alert_name"]
        query = RunbookSearchQuery(query=alert_name, service=service, top_k=5)
        result = deps.runbook_search_tool.run(query)
        deps.tool_call_recorder(
            agent_run_id=state["agent_run_id"],
            node_name="retrieve_runbook",
            tool_name=deps.runbook_search_tool.name,
            query=query,
            result=result,
            input_summary=f"q={alert_name} service={service}",
        )
        chunks = result.data.get("results", []) if isinstance(result.data, dict) else []
        deps.node_tracer(
            node_id=node_id,
            agent_run_id=state["agent_run_id"],
            name="retrieve_runbook",
            status=result.status,
            started_at=started_at,
            finished_at=utc_now(),
            input_summary=f"query={alert_name}",
            output_summary=f"found {len(chunks)} chunks",
        )
        return {**state, "runbook_context": chunks, "phase": "runbook_retrieved"}
    except Exception as exc:
        deps.node_tracer(
            node_id=node_id,
            agent_run_id=state["agent_run_id"],
            name="retrieve_runbook",
            status="failed",
            started_at=started_at,
            finished_at=utc_now(),
            error_message=str(exc),
        )
        state.setdefault("errors", []).append({"node": "retrieve_runbook", "error": str(exc)})
        return state
