"""LLM diagnosis node with JSON retry and rules-based fallback."""

from __future__ import annotations

import json
from copy import deepcopy

from packages.agent.evidence_validation import (
    apply_confidence_adjustment,
    cross_validate_state,
)
from packages.agent.llm.base import extract_json
from packages.agent.llm.reasoning import (
    capture_metadata,
    format_call_metadata,
    record_llm_call,
    should_use_deep_reasoning,
)
from packages.agent.prompts import DIAGNOSIS_PROMPT_TEMPLATE
from packages.agent.schemas import AgentDeps, DiagnosisOutput
from packages.agent.state import IncidentState
from packages.agent.topology import analyze_cascade_from_state
from packages.common.ids import new_id
from packages.common.time import utc_now

_NODE_NAME = "diagnose"


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
                    state.get("metrics_evidence", [])
                    + state.get("logs_evidence", [])
                    + state.get("traces_evidence", [])
                    + state.get("deployment_evidence", [])
                    + state.get("k8s_evidence", [])
                    + state.get("db_evidence", [])
                ),
                runbook_block=json.dumps(state.get("runbook_context", [])),
                memory_block=json.dumps(state.get("memory_context", [])),
            )

        # diagnose is the core reasoning node — request deep reasoning per config.
        thinking = should_use_deep_reasoning(deps.settings, _NODE_NAME)

        _clear_llm_metadata(deps.llm)
        try:
            output = deps.llm.generate_json(prompt_text, DiagnosisOutput, thinking=thinking)
        except Exception:
            try:
                repair_prompt = (
                    "Return only a valid JSON object matching DiagnosisOutput. "
                    "Preserve evidence_id references from the original prompt.\n\n"
                    f"Original prompt:\n{prompt_text}"
                )
                raw = deps.llm.invoke(
                    [{"role": "user", "content": repair_prompt}],
                    thinking=False,  # disable reasoning on retry — cleaner output
                )
                data = extract_json(raw)
                output = DiagnosisOutput(**data)
            except Exception:
                output = _rules_diagnosis(
                    state.get("alert_name", ""), _state_evidence_ids(state)
                )

        hypotheses = [h.model_dump() for h in output.hypotheses]
        root_cause = dict(output.root_cause)
        rationale = _build_rationale(output, hypotheses)

        # Cross-validate evidence: corroboration raises confidence, conflict flags
        # the run for human review, missing sources are recorded but never block.
        cross_validation = cross_validate_state(state)
        adjustment = cross_validation["confidence_adjustment"]
        # Label confidence provenance so downstream consumers can tell a
        # model-reported confidence apart from one the cross-validation step
        # adjusted (and recover the original value for audit).
        if adjustment:
            base_conf = float(root_cause.get("confidence", 0) or 0)
            root_cause["model_confidence"] = base_conf
            root_cause["confidence"] = apply_confidence_adjustment(base_conf, adjustment)
            root_cause["confidence_adjustment"] = adjustment
            root_cause["confidence_source"] = "cross_validated"
        else:
            root_cause["confidence_source"] = "model"
        needs_review = bool(cross_validation["needs_human_review"])

        # Cascading-failure analysis is informational and does not change decisions.
        cascade_analysis = analyze_cascade_from_state(
            state, topology_path=deps.settings.service_topology_path
        )

        meta = capture_metadata(deps.llm)
        record_llm_call(state, _NODE_NAME, meta)
        meta_summary = format_call_metadata(meta)

        deps.node_tracer(
            node_id=node_id,
            agent_run_id=state["agent_run_id"],
            name="diagnose",
            status="succeeded",
            started_at=started_at,
            finished_at=utc_now(),
            input_summary=f"diagnosis prompt thinking={thinking}",
            output_summary=(
                f"hypotheses={len(hypotheses)} rc={root_cause.get('summary', '')[:80]} "
                f"xval={cross_validation['status']} cascade={cascade_analysis['is_cascade']} "
                f"{meta_summary}"
            ).strip(),
        )
        return {
            **state,
            "hypotheses": hypotheses,
            "root_cause": root_cause,
            "diagnosis_rationale": rationale,
            "cross_validation": cross_validation,
            "needs_human_review": needs_review,
            "cascade_analysis": cascade_analysis,
            "llm_calls": state.get("llm_calls", []),
            "phase": "diagnosed",
        }
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
        errors = list(state.get("errors", []))
        errors.append({"node": "diagnose", "error": str(exc)})
        return {**state, "errors": errors}


def _build_rationale(
    output: DiagnosisOutput, hypotheses: list[dict[str, object]]
) -> dict[str, object]:
    """Structured, auditable rationale that cites evidence IDs (Phase 1.2).

    Derived from the structured diagnosis output — never from raw model
    chain-of-thought, which is not persisted.
    """
    root_cause = output.root_cause
    root_evidence = root_cause.get("evidence_ids") or output.evidence_ids
    return {
        "root_cause": root_cause.get("summary", ""),
        "root_cause_confidence": root_cause.get("confidence", 0),
        "evidence_ids": list(root_evidence),
        "hypothesis_ranking": [
            {
                "id": h.get("id", ""),
                "confidence": h.get("confidence", 0),
                "why": h.get("rank_explanation", ""),
                "evidence_ids": h.get("supporting_evidence_ids", []),
            }
            for h in hypotheses
        ],
        "missing_evidence": list(output.missing_evidence),
    }


def _state_evidence_ids(state: IncidentState) -> list[str]:
    ids: list[str] = []
    for key in (
        "metrics_evidence",
        "logs_evidence",
        "traces_evidence",
        "deployment_evidence",
    ):
        for item in state.get(key, []) or []:
            evidence_id = item.get("evidence_id") if isinstance(item, dict) else None
            if isinstance(evidence_id, str) and evidence_id and evidence_id not in ids:
                ids.append(evidence_id)
    return ids


def _clear_llm_metadata(llm: object) -> None:
    if hasattr(llm, "last_metadata"):
        try:
            llm.last_metadata = {}
        except Exception:
            pass


def _rules_diagnosis(alert_name: str, evidence_ids: list[str] | None = None) -> DiagnosisOutput:
    from packages.agent.fake_llm import _DIAGNOSIS_MAP

    data = deepcopy(_DIAGNOSIS_MAP.get(alert_name, _DIAGNOSIS_MAP["High5xxAfterDeploy"]))
    ids = list(evidence_ids or [])
    if ids:
        data["evidence_ids"] = ids
        root_cause = data.setdefault("root_cause", {})
        root_cause["evidence_ids"] = ids
        for hypothesis in data.get("hypotheses", []) or []:
            if not hypothesis.get("supporting_evidence_ids"):
                hypothesis["supporting_evidence_ids"] = ids
    return DiagnosisOutput(**data)
