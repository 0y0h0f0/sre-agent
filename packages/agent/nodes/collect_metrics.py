"""Collect Prometheus metrics evidence."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from packages.agent.nodes._persist import persist_evidence
from packages.agent.schemas import AgentDeps
from packages.agent.state import IncidentState
from packages.common.ids import new_id
from packages.common.time import utc_now
from packages.tools.metrics import MetricsQuery


def collect_metrics(state: IncidentState, deps: AgentDeps) -> IncidentState:
    node_id = new_id("nd_")
    started_at = utc_now()
    try:
        service = state.get("service_name", "unknown")
        alert_name = state.get("alert_name", "UnknownAlert")
        tw = state.get("time_window", {})
        start = datetime.fromisoformat(tw["start"])
        end = datetime.fromisoformat(tw["end"])
        metric_type = _metric_for_alert(alert_name)
        query = MetricsQuery(service=service, metric_type=metric_type, start=start, end=end)
        result = deps.metrics_tool.run(query)
        deps.tool_call_recorder(
            agent_run_id=state["agent_run_id"],
            node_name="collect_metrics",
            tool_name=deps.metrics_tool.name,
            query=query,
            result=result,
            input_summary=f"metric={metric_type} service={service}",
        )
        evidence = (
            result.evidence
            if result.evidence
            else [
                {
                    "type": "metric",
                    "source": "prometheus",
                    "metric_type": metric_type,
                    "service": service,
                    "status": result.status,
                    "summary": result.summary,
                }
            ]
        )

        # Persist evidence to DB
        persist_evidence(deps.db, state["incident_id"], state["agent_run_id"], evidence)

        deps.node_tracer(
            node_id=node_id,
            agent_run_id=state["agent_run_id"],
            name="collect_metrics",
            status=result.status,
            started_at=started_at,
            finished_at=utc_now(),
            input_summary=f"metric={metric_type}",
            output_summary=result.summary,
        )
        return {**state, "metrics_evidence": evidence, "phase": "metrics_collected"}
    except Exception as exc:
        deps.node_tracer(
            node_id=node_id,
            agent_run_id=state["agent_run_id"],
            name="collect_metrics",
            status="failed",
            started_at=started_at,
            finished_at=utc_now(),
            error_message=str(exc),
        )
        state.setdefault("errors", []).append({"node": "collect_metrics", "error": str(exc)})
        return state


MetricType = Literal[
    "latency",
    "error_rate",
    "qps",
    "cpu",
    "memory",
    "db_connections",
    "cache_hit_rate",
]


def _metric_for_alert(alert_name: str) -> MetricType:
    n = alert_name.lower()
    if "db" in n or "connection" in n:
        return "db_connections"
    if "cache" in n or "redis" in n:
        return "cache_hit_rate"
    if "pod" in n or "restart" in n:
        return "memory"
    return "error_rate"
