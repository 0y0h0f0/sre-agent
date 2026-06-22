"""Generate post-incident report with evidence references."""

from __future__ import annotations

import json

from packages.agent.llm.base import extract_json
from packages.agent.llm.profiles import REPORT_PROFILE
from packages.agent.llm.reasoning import (
    capture_metadata,
    llm_profile_call_options,
    record_llm_call,
    should_use_deep_reasoning,
)
from packages.agent.schemas import AgentDeps
from packages.agent.state import IncidentState
from packages.common import metrics as agent_metrics
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
            + state.get("k8s_evidence", [])
            + state.get("db_evidence", [])
            + state.get("verify_evidence", [])
            + _runbook_context_evidence(state.get("runbook_context", []))
        )
        report_context, report_compression = (
            deps.context_builder.compressor.compress_report_inputs(
                evidence=evidence,
                actions=actions,
                errors=state.get("errors", []),
            )
        )
        if deps.settings.llm_deterministic_report_enabled:
            report_data = _fallback_report(state, root_cause, actions, evidence)
        else:
            root_summary = root_cause.get("summary", "")
            root_confidence = root_cause.get("confidence", 0)
            prompt = (
                f"Generate an incident report.\n"
                f"Incident: {state.get('incident_id', '')}\n"
                f"Service: {state.get('service_name', '')}\n"
                f"Root cause: {root_summary} (confidence: {root_confidence})\n"
                f"Compressed report context: {json.dumps(report_context, default=str)}\n"
                "Every evidence-backed claim must cite evidence_id values from the evidence "
                "list.\n"
                "If summarized evidence was omitted from prompt detail, use omitted_evidence_ids "
                "only for traceability and do not invent facts about omitted details.\n"
            )

            thinking = should_use_deep_reasoning(deps.settings, _NODE_NAME)
            profile_options = llm_profile_call_options(
                deps.settings,
                REPORT_PROFILE,
                aliases=(_NODE_NAME,),
            )
            try:
                raw = deps.llm.invoke(
                    [{"role": "user", "content": prompt}],
                    thinking=thinking,
                    **profile_options,
                )
                record_llm_call(state, _NODE_NAME, capture_metadata(deps.llm))
                report_data = extract_json(raw)
            except Exception:
                agent_metrics.AgentMetricsCollector.record_llm_fallback(
                    node=_NODE_NAME,
                    reason="report_generation_failed",
                )
                report_data = _fallback_report(state, root_cause, actions, evidence)

        # Surface the deterministic cross-validation review flag (Phase 1.3).
        # It is authoritative, so it is injected here rather than trusted to the
        # LLM output, and a follow-up is added so reviewers can act on it.
        report_data = dict(report_data)
        report_data["evidence_ids"] = _merge_strings(
            report_data.get("evidence_ids"),
            report_context.get("all_evidence_ids"),
            root_cause.get("evidence_ids"),
            state.get("evidence_ids"),
        )
        report_data["runbook_chunk_ids"] = _merge_strings(
            report_data.get("runbook_chunk_ids"),
            report_context.get("runbook_chunk_ids"),
            root_cause.get("runbook_chunk_ids"),
            state.get("runbook_chunk_ids"),
        )
        report_data["verify_result"] = state.get("verify_result", "")
        report_data["verify_gates"] = state.get("verify_gates", [])
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
            root_cause=_as_text(report_data.get("root_cause", root_cause.get("summary", ""))),
            impact=_as_text(report_data.get("impact", "unknown")),
            timeline=report_data.get("timeline", []),
            actions=report_data.get("actions", actions),
            follow_ups=report_data.get("follow_ups", []),
            body_markdown=json.dumps(report_data, indent=2),
        )
        deps.db.flush()

        deps.node_tracer(
            node_id=node_id,
            agent_run_id=state["agent_run_id"],
            name="generate_report",
            status="succeeded",
            started_at=started_at,
            finished_at=utc_now(),
            input_summary=(
                f"evidence={len(evidence)} actions={len(actions)} "
                f"report_tokens={report_compression.before_tokens}->{report_compression.after_tokens}"
            ),
            output_summary=f"report_id={report.report_id} v{version}",
        )
        compression_events = list(state.get("compression_events", []))
        compression_events.append({
            **report_compression.model_dump(),
            "scope": "report_generation",
        })
        return {
            **state,
            "incident_report": {
                "report_id": report.report_id,
                "version": version,
                **report_data,
            },
            "compression_events": compression_events,
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
        raise


def _as_text(value: object) -> str:
    """Coerce an LLM-produced field into the plain string the report column needs.

    Real providers sometimes return a structured object (e.g. ``root_cause`` as
    ``{"description": ..., "confidence": ...}``) where the schema expects a
    string. Prefer a human summary key, otherwise fall back to compact JSON.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("summary", "description", "text"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate:
                return candidate
        return json.dumps(value, ensure_ascii=False)
    if value is None:
        return ""
    return str(value)


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
        "runbook_chunk_ids": _runbook_chunk_ids_from_evidence(evidence),
        "follow_ups": ["Review monitoring", "Update runbook"],
    }


def _runbook_context_evidence(chunks: list[dict[str, object]]) -> list[dict[str, object]]:
    evidence: list[dict[str, object]] = []
    for chunk in chunks:
        if not isinstance(chunk, dict) or not chunk.get("evidence_id"):
            continue
        evidence.append(
            {
                "evidence_id": chunk.get("evidence_id"),
                "type": "runbook",
                "source": "runbook",
                "source_id": chunk.get("source_id") or chunk.get("chunk_id"),
                "title": chunk.get("title", "Runbook match"),
                "summary": chunk.get("excerpt", ""),
                "confidence": chunk.get("score"),
                "payload": {
                    "chunk_id": chunk.get("chunk_id"),
                    "source_path": chunk.get("source_path"),
                    "metadata": chunk.get("metadata", {}),
                    "score": chunk.get("score"),
                },
            }
        )
    return evidence


def _merge_strings(*values: object) -> list[str]:
    merged: list[str] = []
    for value in values:
        if isinstance(value, str):
            candidates = [value]
        elif isinstance(value, list | tuple | set):
            candidates = list(value)
        else:
            candidates = []
        for candidate in candidates:
            if isinstance(candidate, str) and candidate and candidate not in merged:
                merged.append(candidate)
    return merged


def _runbook_chunk_ids_from_evidence(evidence: list[dict[str, object]]) -> list[str]:
    chunk_ids: list[str] = []
    for item in evidence:
        for value in (item.get("runbook_chunk_ids"), item.get("runbook_chunks")):
            if isinstance(value, list):
                for chunk_id in value:
                    text = str(chunk_id)
                    if text and text not in chunk_ids:
                        chunk_ids.append(text)
        payload = item.get("payload")
        if isinstance(payload, dict):
            chunk_id = payload.get("chunk_id")
            if chunk_id and str(chunk_id) not in chunk_ids:
                chunk_ids.append(str(chunk_id))
    return chunk_ids
