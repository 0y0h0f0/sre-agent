"""Collect database read-only diagnosis evidence (roadmap Phase 2.3).

No-op when ``deps.db_diagnostics_tool`` is absent (e.g. the eval harness).
"""

from __future__ import annotations

from packages.agent.schemas import AgentDeps
from packages.agent.state import IncidentState
from packages.common.ids import new_id
from packages.common.time import utc_now
from packages.tools.db_diagnostics import DbDiagnosticsQuery

# Fault classes that implicate the database layer. Slow/latency faults are
# included because a saturated pool or slow queries are common upstream causes.
_DB_KEYWORDS = (
    "db",
    "database",
    "connection",
    "pool",
    "query",
    "lock",
    "deadlock",
    "slow",
    "latency",
)
_TOP_SEVERITIES = {"P0", "SEV1", "CRITICAL"}


def _db_relevant(alert_name: str, severity: str) -> bool:
    if severity.strip().upper() in _TOP_SEVERITIES:
        return True
    name = alert_name.lower()
    return any(keyword in name for keyword in _DB_KEYWORDS)


def collect_db(state: IncidentState, deps: AgentDeps) -> IncidentState:
    if deps.db_diagnostics_tool is None or not _db_relevant(
        state.get("alert_name", ""), state.get("severity", "")
    ):
        return {**state, "db_evidence": [], "phase": "db_collected"}

    node_id = new_id("nd_")
    started_at = utc_now()
    try:
        query = DbDiagnosticsQuery(operation="connection_pool")
        result = deps.db_diagnostics_tool.run(query)
        deps.tool_call_recorder(
            agent_run_id=state["agent_run_id"],
            node_name="collect_db",
            tool_name=deps.db_diagnostics_tool.name,
            query=query,
            result=result,
            input_summary="op=connection_pool",
        )
        evidence = (
            result.evidence
            if result.evidence
            else [
                {
                    "type": "db",
                    "source": "db",
                    "status": result.status,
                    "summary": result.summary,
                }
            ]
        )
        deps.node_tracer(
            node_id=node_id,
            agent_run_id=state["agent_run_id"],
            name="collect_db",
            status=result.status,
            started_at=started_at,
            finished_at=utc_now(),
            input_summary="op=connection_pool",
            output_summary=result.summary,
        )
        return {**state, "db_evidence": evidence, "phase": "db_collected"}
    except Exception as exc:
        deps.node_tracer(
            node_id=node_id,
            agent_run_id=state["agent_run_id"],
            name="collect_db",
            status="failed",
            started_at=started_at,
            finished_at=utc_now(),
            error_message=str(exc),
        )
        state.setdefault("errors", []).append({"node": "collect_db", "error": str(exc)})
        return state
