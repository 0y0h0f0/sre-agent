"""Collect Loki logs evidence."""

from __future__ import annotations

from datetime import datetime

from packages.agent.nodes._persist import persist_evidence
from packages.agent.schemas import AgentDeps
from packages.agent.state import IncidentState
from packages.common.ids import new_id
from packages.common.time import utc_now
from packages.tools.logs import LogsQuery


def collect_logs(state: IncidentState, deps: AgentDeps) -> IncidentState:
    node_id = new_id("nd_")
    started_at = utc_now()
    try:
        service = state.get("service_name", "unknown")
        alert_name = state.get("alert_name", "UnknownAlert")
        tw = state.get("time_window", {})
        start = datetime.fromisoformat(tw["start"])
        end = datetime.fromisoformat(tw["end"])
        keywords = _keywords_for_alert(alert_name)
        query = LogsQuery(service=service, start=start, end=end, keywords=keywords, limit=100)
        result = deps.logs_tool.run(query)
        deps.tool_call_recorder(
            agent_run_id=state["agent_run_id"],
            node_name="collect_logs",
            tool_name=deps.logs_tool.name,
            query=query,
            result=result,
            input_summary=f"keywords={keywords} service={service}",
        )
        evidence = (
            result.evidence
            if result.evidence
            else [
                {
                    "type": "log",
                    "source": "loki",
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
            name="collect_logs",
            status=result.status,
            started_at=started_at,
            finished_at=utc_now(),
            input_summary=f"service={service}",
            output_summary=result.summary,
        )
        return {**state, "logs_evidence": evidence, "phase": "logs_collected"}
    except Exception as exc:
        deps.node_tracer(
            node_id=node_id,
            agent_run_id=state["agent_run_id"],
            name="collect_logs",
            status="failed",
            started_at=started_at,
            finished_at=utc_now(),
            error_message=str(exc),
        )
        state.setdefault("errors", []).append({"node": "collect_logs", "error": str(exc)})
        return state


def _keywords_for_alert(alert_name: str) -> list[str]:
    n = alert_name.lower()
    if "db" in n or "connection" in n:
        return ["database", "connection", "exhausted"]
    if "cache" in n or "redis" in n:
        return ["redis", "cache", "miss"]
    if "pod" in n or "restart" in n:
        return ["restart", "oom", "kubernetes"]
    return ["5xx", "error", "deploy"]
