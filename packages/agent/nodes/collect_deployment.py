"""Collect Git changes / deployment evidence."""

from __future__ import annotations

from datetime import datetime, timedelta

from packages.agent.nodes._persist import persist_evidence
from packages.agent.schemas import AgentDeps
from packages.agent.state import IncidentState
from packages.common.ids import new_id
from packages.common.time import utc_now
from packages.tools.git_changes import GitChangeQuery


def collect_deployment(state: IncidentState, deps: AgentDeps) -> IncidentState:
    node_id = new_id("nd_")
    started_at = utc_now()
    try:
        service = state.get("service_name", "unknown")
        tw = state.get("time_window", {})
        start = datetime.fromisoformat(tw["start"]) - timedelta(minutes=30)
        end = datetime.fromisoformat(tw["end"]) + timedelta(minutes=30)
        query = GitChangeQuery(service=service, start=start, end=end)
        result = deps.git_change_tool.run(query)
        deps.tool_call_recorder(
            agent_run_id=state["agent_run_id"],
            node_name="collect_deployment",
            tool_name=deps.git_change_tool.name,
            query=query,
            result=result,
            input_summary=f"service={service}",
        )
        evidence = (
            result.evidence
            if result.evidence
            else [
                {
                    "type": "deployment",
                    "source": "git",
                    "service": service,
                    "status": result.status,
                    "summary": result.summary,
                }
            ]
        )

        evidence = persist_evidence(
            deps.db, state["incident_id"], state["agent_run_id"], evidence
        )

        deps.node_tracer(
            node_id=node_id,
            agent_run_id=state["agent_run_id"],
            name="collect_deployment",
            status=result.status,
            started_at=started_at,
            finished_at=utc_now(),
            input_summary=f"service={service}",
            output_summary=result.summary,
        )
        return {**state, "deployment_evidence": evidence, "phase": "deployment_collected"}
    except Exception as exc:
        deps.node_tracer(
            node_id=node_id,
            agent_run_id=state["agent_run_id"],
            name="collect_deployment",
            status="failed",
            started_at=started_at,
            finished_at=utc_now(),
            error_message=str(exc),
        )
        state.setdefault("errors", []).append({"node": "collect_deployment", "error": str(exc)})
        return state
