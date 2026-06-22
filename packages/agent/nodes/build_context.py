"""Assemble prompt context for the diagnosis LLM call."""

from __future__ import annotations

from packages.agent.prompts import COMPACT_DIAGNOSIS_OUTPUT_INSTRUCTIONS, SYSTEM_PROMPT
from packages.agent.schemas import AgentDeps
from packages.agent.state import IncidentState
from packages.common.ids import new_id
from packages.common.time import utc_now
from packages.memory.schemas import BuildContextInput


def build_context(state: IncidentState, deps: AgentDeps) -> IncidentState:
    node_id = new_id("nd_")
    started_at = utc_now()
    try:
        evidence = (
            state.get("metrics_evidence", [])
            + state.get("logs_evidence", [])
            + state.get("traces_evidence", [])
            + state.get("deployment_evidence", [])
            + state.get("k8s_evidence", [])
            + state.get("db_evidence", [])
        )
        bci = BuildContextInput(
            incident={
                "_system_prompt": SYSTEM_PROMPT,
                "service_name": state.get("service_name", ""),
                "severity": state.get("severity", ""),
                "alert_name": state.get("alert_name", ""),
                "time_window": state.get("time_window", {}),
            },
            evidence=evidence,
            runbook_chunks=state.get("runbook_context", []),
            memories=state.get("memory_context", []),
            cross_incident=state.get("cross_incident_context", []),
            output_schema=f"CompactDiagnosisOutput:v1\n{COMPACT_DIAGNOSIS_OUTPUT_INSTRUCTIONS}",
        )
        built = deps.context_builder.build(bci)
        deps.node_tracer(
            node_id=node_id,
            agent_run_id=state["agent_run_id"],
            name="build_context",
            status="succeeded",
            started_at=started_at,
            finished_at=utc_now(),
            input_summary=f"evidence={len(evidence)} runbook={len(bci.runbook_chunks)}",
            output_summary=f"tokens={built.token_usage_estimate}",
        )
        return {
            **state,
            "token_budget": built.token_usage_estimate,
            "compression_events": [c.model_dump() for c in built.compressed_context],
            "phase": "context_built",
            "_built_messages": built.messages,  # type: ignore[typeddict-unknown-key]
        }
    except Exception as exc:
        deps.node_tracer(
            node_id=node_id,
            agent_run_id=state["agent_run_id"],
            name="build_context",
            status="failed",
            started_at=started_at,
            finished_at=utc_now(),
            error_message=str(exc),
        )
        errors = list(state.get("errors", []))
        errors.append({"node": "build_context", "error": str(exc)})
        return {**state, "errors": errors}
