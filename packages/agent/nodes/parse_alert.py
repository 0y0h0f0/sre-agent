"""Parse alert payload — no LLM call."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from packages.agent.schemas import AgentDeps
from packages.agent.state import IncidentState
from packages.common.ids import new_id
from packages.common.time import ensure_utc, utc_now


def parse_alert(state: IncidentState, deps: AgentDeps) -> IncidentState:
    node_id = new_id("nd_")
    started_at = utc_now()
    try:
        alert = state.get("alert_payload", {})
        service_name = alert.get("service", "unknown")
        severity = alert.get("severity", "P3")
        alert_name = alert.get("alert_name", "UnknownAlert")
        starts_at = _parse_dt(alert.get("starts_at"))
        ends_at_raw = alert.get("ends_at")
        ends_at = _parse_dt(ends_at_raw) if ends_at_raw else None
        time_window = {
            "start": (starts_at - timedelta(minutes=10)).isoformat(),
            "end": (ends_at or utc_now() + timedelta(minutes=5)).isoformat(),
        }
        deps.node_tracer(
            node_id=node_id,
            agent_run_id=state["agent_run_id"],
            name="parse_alert",
            status="succeeded",
            started_at=started_at,
            finished_at=utc_now(),
            input_summary=f"alert={alert_name}",
            output_summary=f"service={service_name} severity={severity}",
        )
        return {
            **state,
            "service_name": service_name,
            "severity": severity,
            "alert_name": alert_name,
            "time_window": time_window,
            "phase": "alert_parsed",
        }
    except Exception as exc:
        deps.node_tracer(
            node_id=node_id,
            agent_run_id=state["agent_run_id"],
            name="parse_alert",
            status="failed",
            started_at=started_at,
            finished_at=utc_now(),
            error_message=str(exc),
        )
        state.setdefault("errors", []).append({"node": "parse_alert", "error": str(exc)})
        return state


def _parse_dt(value: Any) -> datetime:
    """Coerce a datetime or ISO string to a UTC datetime."""
    if isinstance(value, datetime):
        return ensure_utc(value)
    if isinstance(value, str):
        return ensure_utc(datetime.fromisoformat(value))
    return utc_now()
