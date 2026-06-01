"""Generate post-incident report with evidence references."""

from __future__ import annotations

import json

from packages.agent.schemas import AgentDeps
from packages.agent.state import IncidentState
from packages.common.ids import new_id
from packages.common.time import utc_now
from packages.db.repositories.reports import IncidentReportRepository


def generate_report(state: IncidentState, deps: AgentDeps) -> IncidentState:
    node_id = new_id("nd_")
    started_at = utc_now()
    try:
        root_cause = state.get("root_cause", {})
        actions = state.get("recommended_actions", [])
        evidence = (
            state.get("metrics_evidence", [])
            + state.get("logs_evidence", [])
            + state.get("traces_evidence", [])
            + state.get("deployment_evidence", [])
        )
        # Build a richer prompt with evidence and actions
        evidence_summary = json.dumps(
            [
                {
                    "type": e.get("type", ""),
                    "source": e.get("source", ""),
                    "summary": str(e.get("summary", ""))[:200],
                }
                for e in evidence
            ]
        )
        actions_summary = json.dumps(
            [
                {
                    "type": a.get("type", ""),
                    "reason": a.get("reason", ""),
                    "risk_level": a.get("risk_level", ""),
                }
                for a in actions
            ]
        )
        root_summary = root_cause.get("summary", "")
        root_confidence = root_cause.get("confidence", 0)
        prompt = (
            f"Generate an incident report.\n"
            f"Incident: {state.get('incident_id', '')}\n"
            f"Service: {state.get('service_name', '')}\n"
            f"Root cause: {root_summary} (confidence: {root_confidence})\n"
            f"Evidence collected: {evidence_summary}\n"
            f"Actions proposed: {actions_summary}\n"
            f"Errors: {json.dumps(state.get('errors', []))}\n"
        )

        try:
            raw = deps.llm.invoke([{"role": "user", "content": prompt}])
            report_data = json.loads(raw)
        except Exception:
            report_data = _fallback_report(state, root_cause, actions)

        repo = IncidentReportRepository(deps.db)
        version = repo.next_version(state["incident_id"])
        report = repo.create(
            incident_id=state["incident_id"],
            agent_run_id=state["agent_run_id"],
            version=version,
            root_cause=report_data.get("root_cause", root_cause.get("summary", "")),
            impact=report_data.get("impact", "unknown"),
            timeline=report_data.get("timeline", []),
            actions=report_data.get("actions", actions),
            follow_ups=report_data.get("follow_ups", []),
            body_markdown=json.dumps(report_data, indent=2),
        )

        deps.node_tracer(
            node_id=node_id,
            agent_run_id=state["agent_run_id"],
            name="generate_report",
            status="succeeded",
            started_at=started_at,
            finished_at=utc_now(),
            input_summary=f"evidence={len(evidence)} actions={len(actions)}",
            output_summary=f"report_id={report.report_id} v{version}",
        )
        return {
            **state,
            "incident_report": {
                "report_id": report.report_id,
                "version": version,
                **report_data,
            },
            "phase": "report_generated",
        }
    except Exception as exc:
        deps.node_tracer(
            node_id=node_id,
            agent_run_id=state["agent_run_id"],
            name="generate_report",
            status="failed",
            started_at=started_at,
            finished_at=utc_now(),
            error_message=str(exc),
        )
        state.setdefault("errors", []).append({"node": "generate_report", "error": str(exc)})
        return state


def _fallback_report(
    state: IncidentState, root_cause: dict[str, object], actions: list[dict[str, object]]
) -> dict[str, object]:
    return {
        "root_cause": root_cause.get("summary", "unknown"),
        "impact": "Service affected — see evidence",
        "timeline": [
            {"time": state.get("time_window", {}).get("start", ""), "event": "Alert fired"}
        ],
        "actions": actions,
        "follow_ups": ["Review monitoring", "Update runbook"],
    }
