"""Generate post-incident report with evidence references."""

from __future__ import annotations

import json

from packages.agent.llm.base import extract_json
from packages.agent.llm.reasoning import (
    capture_metadata,
    record_llm_call,
    should_use_deep_reasoning,
)
from packages.agent.schemas import AgentDeps
from packages.agent.state import IncidentState
from packages.common.ids import new_id
from packages.common.time import utc_now
from packages.db.repositories.reports import IncidentReportRepository

_NODE_NAME = "generate_report"


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
                    "evidence_id": e.get("evidence_id", ""),
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
            "Every evidence-backed claim must cite evidence_id values from the evidence list.\n"
        )

        thinking = should_use_deep_reasoning(deps.settings, _NODE_NAME)
        try:
            raw = deps.llm.invoke([{"role": "user", "content": prompt}], thinking=thinking)
            record_llm_call(state, _NODE_NAME, capture_metadata(deps.llm))
            report_data = extract_json(raw)
        except Exception:
            report_data = _fallback_report(state, root_cause, actions, evidence)

        # Surface the deterministic cross-validation review flag (Phase 1.3).
        # It is authoritative, so it is injected here rather than trusted to the
        # LLM output, and a follow-up is added so reviewers can act on it.
        report_data = dict(report_data)
        needs_review = bool(state.get("needs_human_review", False))
        report_data["needs_human_review"] = needs_review
        if needs_review:
            follow_ups = list(report_data.get("follow_ups", []) or [])
            note = "Manual review required: evidence sources conflict (cross-validation)."
            if note not in follow_ups:
                follow_ups.append(note)
            report_data["follow_ups"] = follow_ups

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
    state: IncidentState,
    root_cause: dict[str, object],
    actions: list[dict[str, object]],
    evidence: list[dict[str, object]],
) -> dict[str, object]:
    evidence_ids = [
        str(item.get("evidence_id"))
        for item in evidence
        if isinstance(item.get("evidence_id"), str) and item.get("evidence_id")
    ]
    return {
        "root_cause": root_cause.get("summary", "unknown"),
        "impact": "Service affected; see referenced evidence ids",
        "timeline": [
            {"time": state.get("time_window", {}).get("start", ""), "event": "Alert fired"}
        ],
        "actions": actions,
        "evidence_ids": evidence_ids,
        "follow_ups": ["Review monitoring", "Update runbook"],
    }
