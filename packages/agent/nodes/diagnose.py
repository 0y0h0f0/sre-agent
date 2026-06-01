"""LLM diagnosis node with JSON retry and rules-based fallback."""

from __future__ import annotations

import json

from packages.agent.prompts import DIAGNOSIS_PROMPT_TEMPLATE
from packages.agent.schemas import AgentDeps, DiagnosisOutput
from packages.agent.state import IncidentState
from packages.common.ids import new_id
from packages.common.time import utc_now


def diagnose(state: IncidentState, deps: AgentDeps) -> IncidentState:
    node_id = new_id("nd_")
    started_at = utc_now()
    try:
        messages = state.get("_built_messages", [])
        prompt_text = " ".join(str(m.get("content", "")) for m in messages) if messages else ""
        if not prompt_text:
            prompt_text = DIAGNOSIS_PROMPT_TEMPLATE.format(
                service_name=state.get("service_name", ""),
                alert_name=state.get("alert_name", ""),
                severity=state.get("severity", ""),
                time_window=state.get("time_window", {}),
                evidence_block=json.dumps(
                    state.get("metrics_evidence", []) + state.get("logs_evidence", [])
                ),
                runbook_block=json.dumps(state.get("runbook_context", [])),
                memory_block=json.dumps(state.get("memory_context", [])),
            )

        try:
            output = deps.llm.generate_json(prompt_text, DiagnosisOutput)
        except Exception:
            try:
                raw = deps.llm.invoke([{"role": "user", "content": prompt_text}])
                data = json.loads(raw)
                output = DiagnosisOutput(**data)
            except Exception:
                output = _rules_diagnosis(state.get("alert_name", ""))

        hypotheses = [h.model_dump() for h in output.hypotheses]
        root_cause = output.root_cause

        deps.node_tracer(
            node_id=node_id,
            agent_run_id=state["agent_run_id"],
            name="diagnose",
            status="succeeded",
            started_at=started_at,
            finished_at=utc_now(),
            input_summary="diagnosis prompt",
            output_summary=f"hypotheses={len(hypotheses)} rc={root_cause.get('summary', '')[:80]}",
        )
        return {**state, "hypotheses": hypotheses, "root_cause": root_cause, "phase": "diagnosed"}
    except Exception as exc:
        deps.node_tracer(
            node_id=node_id,
            agent_run_id=state["agent_run_id"],
            name="diagnose",
            status="failed",
            started_at=started_at,
            finished_at=utc_now(),
            error_message=str(exc),
        )
        state.setdefault("errors", []).append({"node": "diagnose", "error": str(exc)})
        return state


def _rules_diagnosis(alert_name: str) -> DiagnosisOutput:
    from packages.agent.fake_llm import _DIAGNOSIS_MAP

    data = _DIAGNOSIS_MAP.get(alert_name, _DIAGNOSIS_MAP["High5xxAfterDeploy"])
    return DiagnosisOutput(**data)
