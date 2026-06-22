"""LLM diagnosis — JSON retry, rules fallback, multi-perspective sub-agents (Phase 2)."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, wait
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from packages.agent.evidence_validation import (
    apply_confidence_adjustment,
    cross_validate_state,
)
from packages.agent.llm.base import extract_json
from packages.agent.llm.profiles import DIAGNOSE_REASONING_PROFILE
from packages.agent.llm.reasoning import (
    capture_metadata,
    format_call_metadata,
    llm_profile_call_options,
    record_llm_call,
    should_use_diagnosis_reasoning,
)
from packages.agent.prompts import (
    COMPACT_DIAGNOSIS_OUTPUT_INSTRUCTIONS,
    DIAGNOSIS_PROMPT_TEMPLATE,
    LOGS_SPECIALIST_SYSTEM_PROMPT,
    METRICS_SPECIALIST_SYSTEM_PROMPT,
    SPECIALIST_PROMPT_TEMPLATE,
    SYNTHESIZER_PROMPT_TEMPLATE,
    SYNTHESIZER_SYSTEM_PROMPT,
    TRACES_SPECIALIST_SYSTEM_PROMPT,
)
from packages.agent.schemas import (
    AgentDeps,
    CompactDiagnosisOutput,
    DiagnosisOutput,
    diagnosis_output_from_compact,
)
from packages.agent.state import IncidentState
from packages.agent.topology import analyze_cascade_from_state
from packages.common import metrics as agent_metrics
from packages.common.ids import new_id
from packages.common.time import utc_now

_NODE_NAME = "diagnose"
_SPECIALIST_NODE_NAMES: dict[str, str] = {
    "metrics": "diagnose_metrics",
    "logs": "diagnose_logs",
    "traces": "diagnose_traces",
    "synthesizer": "diagnose_synthesize",
}


def diagnose(state: IncidentState, deps: AgentDeps) -> IncidentState:
    node_id = new_id("nd_")
    started_at = utc_now()
    try:
        if _multi_perspective_enabled(deps):
            output, specialist_summaries = _multi_perspective_diagnose(state, deps)
            multi = True
        else:
            output = _single_call_diagnose(state, deps)
            specialist_summaries = []
            multi = False

        # ---- Post-diagnosis processing (identical for both paths) ----
        hypotheses = [h.model_dump() for h in output.hypotheses]
        root_cause = dict(output.root_cause)
        if output.runbook_chunk_ids and not root_cause.get("runbook_chunk_ids"):
            root_cause["runbook_chunk_ids"] = list(output.runbook_chunk_ids)
        rationale = _build_rationale(output, hypotheses)

        cross_validation = cross_validate_state(state)
        adjustment = cross_validation["confidence_adjustment"]
        if adjustment:
            base_conf = float(root_cause.get("confidence", 0) or 0)
            root_cause["model_confidence"] = base_conf
            root_cause["confidence"] = apply_confidence_adjustment(base_conf, adjustment)
            root_cause["confidence_adjustment"] = adjustment
            root_cause["confidence_source"] = "cross_validated"
        else:
            root_cause["confidence_source"] = "model"
        needs_review = bool(cross_validation["needs_human_review"])

        cascade_analysis = analyze_cascade_from_state(
            state, topology_path=deps.settings.service_topology_path
        )

        meta_summary = ""
        if not multi:
            meta = capture_metadata(deps.llm)
            record_llm_call(state, _NODE_NAME, meta)
            meta_summary = format_call_metadata(meta)

        output_summary = (
            f"hypotheses={len(hypotheses)} rc={root_cause.get('summary', '')[:80]} "
            f"xval={cross_validation['status']} cascade={cascade_analysis['is_cascade']} "
            f"multi_perspective={multi} "
        )
        if specialist_summaries:
            output_summary += "specialists=" + ",".join(specialist_summaries)
        if meta_summary:
            output_summary += f" {meta_summary}"

        deps.node_tracer(
            node_id=node_id,
            agent_run_id=state["agent_run_id"],
            name="diagnose",
            status="succeeded",
            started_at=started_at,
            finished_at=utc_now(),
            input_summary=f"diagnosis prompt multi_perspective={multi}",
            output_summary=output_summary.strip(),
        )
        return {
            **state,
            "hypotheses": hypotheses,
            "root_cause": root_cause,
            "evidence_ids": list(output.evidence_ids),
            "runbook_chunk_ids": list(output.runbook_chunk_ids),
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


# ---------------------------------------------------------------------------
# Multi-perspective sub-agent dispatch (Phase 2)
# ---------------------------------------------------------------------------


def _multi_perspective_enabled(deps: AgentDeps) -> bool:
    return bool(deps.settings.llm_multi_perspective_enabled)


def _multi_perspective_parallel_enabled(deps: AgentDeps) -> bool:
    return bool(
        deps.settings.llm_multi_perspective_parallel_enabled
        and _supports_call_local_metadata(deps.llm)
    )


def _supports_call_local_metadata(llm: Any) -> bool:
    if not hasattr(llm, "generate_json_with_metadata"):
        return False
    delegate = getattr(llm, "delegate", None)
    if delegate is not None and not hasattr(delegate, "generate_json_with_metadata"):
        return False
    return True


@dataclass
class _SpecialistRunResult:
    perspective: str
    node_name: str
    output: DiagnosisOutput = field(default_factory=DiagnosisOutput)
    metadata: dict[str, Any] = field(default_factory=dict)


def _multi_perspective_diagnose(
    state: IncidentState, deps: AgentDeps
) -> tuple[DiagnosisOutput, list[str]]:
    """Run 3 specialists plus a sequential synthesizer."""
    summaries: list[str] = []
    topology = _load_topology(state, deps)

    if _multi_perspective_parallel_enabled(deps):
        metrics_output, logs_output, traces_output = _run_specialists_parallel(
            state,
            deps,
            topology,
            summaries,
        )
    else:
        metrics_output = _run_specialist(
            state, deps, "metrics",
            state.get("metrics_evidence", []),
            METRICS_SPECIALIST_SYSTEM_PROMPT,
        )
        summaries.append(f"metrics:h={len(metrics_output.hypotheses)}")

        logs_output = _run_specialist(
            state, deps, "logs",
            state.get("logs_evidence", []),
            LOGS_SPECIALIST_SYSTEM_PROMPT,
        )
        summaries.append(f"logs:h={len(logs_output.hypotheses)}")

        traces_output = _run_specialist(
            state, deps, "traces",
            state.get("traces_evidence", []) + topology,
            TRACES_SPECIALIST_SYSTEM_PROMPT,
        )
        summaries.append(f"traces:h={len(traces_output.hypotheses)}")

    synthesizer_output = _run_synthesizer(
        state, deps,
        metrics_output, logs_output, traces_output,
    )
    summaries.append(f"synthesizer:h={len(synthesizer_output.hypotheses)}")

    return synthesizer_output, summaries


def _run_specialists_parallel(
    state: IncidentState,
    deps: AgentDeps,
    topology: list[dict[str, Any]],
    summaries: list[str],
) -> tuple[DiagnosisOutput, DiagnosisOutput, DiagnosisOutput]:
    specs = [
        (
            "metrics",
            state.get("metrics_evidence", []),
            METRICS_SPECIALIST_SYSTEM_PROMPT,
        ),
        ("logs", state.get("logs_evidence", []), LOGS_SPECIALIST_SYSTEM_PROMPT),
        (
            "traces",
            state.get("traces_evidence", []) + topology,
            TRACES_SPECIALIST_SYSTEM_PROMPT,
        ),
    ]
    snapshot = _specialist_state_snapshot(state)
    executor = ThreadPoolExecutor(max_workers=3, thread_name_prefix="diagnose-specialist")
    futures = {
        executor.submit(
            _run_specialist_result,
            snapshot,
            deps.llm,
            perspective,
            evidence,
            system_prompt,
            True,
        ): perspective
        for perspective, evidence, system_prompt in specs
    }
    try:
        done, not_done = wait(
            futures,
            timeout=max(0.1, float(deps.settings.llm_timeout_seconds)),
        )
        results: dict[str, _SpecialistRunResult] = {}
        for future in done:
            perspective = futures[future]
            try:
                results[perspective] = future.result()
            except Exception:
                agent_metrics.AgentMetricsCollector.record_llm_fallback(
                    node=_SPECIALIST_NODE_NAMES[perspective],
                    reason="llm_generate_failed",
                )
                results[perspective] = _SpecialistRunResult(
                    perspective=perspective,
                    node_name=_SPECIALIST_NODE_NAMES[perspective],
                )
        for future in not_done:
            perspective = futures[future]
            future.cancel()
            agent_metrics.AgentMetricsCollector.record_llm_fallback(
                node=_SPECIALIST_NODE_NAMES[perspective],
                reason="llm_generate_failed",
            )
            results[perspective] = _SpecialistRunResult(
                perspective=perspective,
                node_name=_SPECIALIST_NODE_NAMES[perspective],
            )
    finally:
        executor.shutdown(wait=False, cancel_futures=True)

    outputs: list[DiagnosisOutput] = []
    for perspective, _evidence, _system_prompt in specs:
        result = results.get(
            perspective,
            _SpecialistRunResult(
                perspective=perspective,
                node_name=_SPECIALIST_NODE_NAMES[perspective],
            ),
        )
        if result.metadata:
            record_llm_call(state, result.node_name, result.metadata)
        outputs.append(result.output)
        summaries.append(f"{perspective}:h={len(result.output.hypotheses)}")
    return outputs[0], outputs[1], outputs[2]


def _run_specialist(
    state: IncidentState,
    deps: AgentDeps,
    perspective: str,
    evidence: list[dict[str, Any]],
    system_prompt: str,
) -> DiagnosisOutput:
    """Run one specialist sub-agent on its evidence type."""
    result = _run_specialist_result(
        state,
        deps.llm,
        perspective,
        evidence,
        system_prompt,
        False,
    )
    if result.metadata:
        record_llm_call(state, result.node_name, result.metadata)
    return result.output


def _run_specialist_result(
    state: IncidentState,
    llm: Any,
    perspective: str,
    evidence: list[dict[str, Any]],
    system_prompt: str,
    require_call_local_metadata: bool,
) -> _SpecialistRunResult:
    """Run one specialist without mutating shared graph state."""
    node_name = _SPECIALIST_NODE_NAMES[perspective]
    prompt_text = SPECIALIST_PROMPT_TEMPLATE.format(
        perspective=perspective,
        service_name=state.get("service_name", ""),
        alert_name=state.get("alert_name", ""),
        severity=state.get("severity", ""),
        time_window=state.get("time_window", {}),
        evidence_block=json.dumps(evidence) if evidence else "No evidence collected.",
        compact_schema=COMPACT_DIAGNOSIS_OUTPUT_INSTRUCTIONS,
    )
    tagged_prompt = f"[perspective:{perspective}]\n{system_prompt}\n\n{prompt_text}"

    if not require_call_local_metadata:
        _clear_llm_metadata(llm)
    try:
        raw_output, metadata = _generate_json_with_metadata(
            llm,
            tagged_prompt,
            CompactDiagnosisOutput,
            thinking=False,
            require_call_local_metadata=require_call_local_metadata,
        )
        output = diagnosis_output_from_compact(raw_output)
        return _SpecialistRunResult(
            perspective=perspective,
            node_name=node_name,
            output=output,
            metadata=metadata,
        )
    except Exception:
        agent_metrics.AgentMetricsCollector.record_llm_fallback(
            node=node_name,
            reason="llm_generate_failed",
        )
        return _SpecialistRunResult(
            perspective=perspective,
            node_name=node_name,
        )


def _run_synthesizer(
    state: IncidentState,
    deps: AgentDeps,
    metrics_output: DiagnosisOutput,
    logs_output: DiagnosisOutput,
    traces_output: DiagnosisOutput,
) -> DiagnosisOutput:
    """Run the synthesizer that integrates all perspectives."""
    node_name = _SPECIALIST_NODE_NAMES["synthesizer"]

    additional = (
        state.get("deployment_evidence", [])
        + state.get("k8s_evidence", [])
        + state.get("db_evidence", [])
    )

    prompt_text = SYNTHESIZER_PROMPT_TEMPLATE.format(
        service_name=state.get("service_name", ""),
        alert_name=state.get("alert_name", ""),
        severity=state.get("severity", ""),
        metrics_output=_serialize_partial_output(metrics_output),
        logs_output=_serialize_partial_output(logs_output),
        traces_output=_serialize_partial_output(traces_output),
        additional_evidence_block=(
            json.dumps(additional) if additional else "No additional evidence."
        ),
        runbook_block=json.dumps(state.get("runbook_context", [])),
        memory_block=json.dumps(state.get("memory_context", [])),
        compact_schema=COMPACT_DIAGNOSIS_OUTPUT_INSTRUCTIONS,
    )
    tagged_prompt = f"[perspective:synthesizer]\n{SYNTHESIZER_SYSTEM_PROMPT}\n\n{prompt_text}"

    reasoning_context = _diagnosis_reasoning_context(state, deps)
    thinking = should_use_diagnosis_reasoning(
        deps.settings,
        "diagnose_synthesize",
        state,
        **reasoning_context,
    )
    profile_options = (
        llm_profile_call_options(
            deps.settings,
            DIAGNOSE_REASONING_PROFILE,
            aliases=("diagnose_synthesize",),
        )
        if thinking
        else {}
    )

    _clear_llm_metadata(deps.llm)
    try:
        raw_output = deps.llm.generate_json(
            tagged_prompt,
            CompactDiagnosisOutput,
            thinking=thinking,
            **profile_options,
        )
        output = diagnosis_output_from_compact(raw_output)
    except Exception:
        agent_metrics.AgentMetricsCollector.record_llm_json_repair_attempt(
            node=node_name,
        )
        try:
            repair_prompt = (
                "Return only valid compact diagnosis JSON. "
                "Preserve evidence_id references from the original prompt.\n\n"
                f"Original prompt:\n{tagged_prompt}"
            )
            raw = deps.llm.invoke(
                [{"role": "user", "content": repair_prompt}],
                thinking=False,
            )
            data = extract_json(raw)
            output = diagnosis_output_from_compact(data)
        except Exception:
            agent_metrics.AgentMetricsCollector.record_llm_fallback(
                node=node_name,
                reason="json_repair_failed",
            )
            output = _single_call_diagnose(
                state, deps,
                specialist_outputs=(metrics_output, logs_output, traces_output),
            )
            meta = capture_metadata(deps.llm)
            record_llm_call(state, _NODE_NAME, meta)
            return output

    meta = capture_metadata(deps.llm)
    record_llm_call(state, node_name, meta)
    return output  # type: ignore[no-any-return]


def _serialize_partial_output(output: DiagnosisOutput) -> str:
    if not output.hypotheses:
        return (
            '{"hypotheses": [], "root_cause": {}, "evidence_ids": [], '
            '"missing_evidence": ["specialist returned no results"]}'
        )
    return json.dumps({
        "hypotheses": [h.model_dump() for h in output.hypotheses],
        "root_cause": output.root_cause,
        "evidence_ids": output.evidence_ids,
        "runbook_chunk_ids": output.runbook_chunk_ids,
        "missing_evidence": output.missing_evidence,
    })


def _specialist_state_snapshot(state: IncidentState) -> IncidentState:
    keys = (
        "service_name",
        "alert_name",
        "severity",
        "time_window",
    )
    return {key: deepcopy(state.get(key)) for key in keys}  # type: ignore[return-value]


def _generate_json_with_metadata(
    llm: Any,
    prompt: str,
    output_schema: Any,
    *,
    thinking: bool,
    require_call_local_metadata: bool,
    **kwargs: Any,
) -> tuple[Any, dict[str, Any]]:
    if hasattr(llm, "generate_json_with_metadata"):
        output, metadata = llm.generate_json_with_metadata(
            prompt,
            output_schema,
            thinking=thinking,
            **kwargs,
        )
        return output, dict(metadata or {})
    if require_call_local_metadata:
        raise RuntimeError("LLM provider does not expose call-local metadata")
    output = llm.generate_json(prompt, output_schema, thinking=thinking, **kwargs)
    return output, capture_metadata(llm)


def _load_topology(state: IncidentState, deps: AgentDeps) -> list[dict[str, Any]]:
    try:
        path = deps.settings.service_topology_path
        if path:
            from pathlib import Path

            topo_path = Path(path)
            if topo_path.exists():
                return [{
                    "type": "topology",
                    "source": "topology_config",
                    "payload": json.loads(topo_path.read_text()),
                    "evidence_id": "topology:latest",
                }]
    except (OSError, ValueError) as exc:
        import logging

        logging.getLogger(__name__).warning("Failed to load topology %s: %s", path, exc)
    return []


# ---------------------------------------------------------------------------
# Single-call diagnosis (original path, preserved as fallback)
# ---------------------------------------------------------------------------


def _single_call_diagnose(
    state: IncidentState,
    deps: AgentDeps,
    specialist_outputs: tuple[DiagnosisOutput, DiagnosisOutput, DiagnosisOutput] | None = None,
) -> DiagnosisOutput:
    """Original monolithic LLM call (preserved as default and fallback).

    When called as a fallback from the multi-perspective synthesizer,
    *specialist_outputs* is a (metrics, logs, traces) tuple whose partial
    results are included in the prompt so successful specialist analysis
    is not discarded.
    """
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
            compact_schema=COMPACT_DIAGNOSIS_OUTPUT_INSTRUCTIONS,
        )

    # When falling back from multi-perspective, include successful specialist
    # outputs so their analysis is not discarded.
    if specialist_outputs:
        parts = [prompt_text, "\n\n## Specialist Analyses (from multi-perspective fallback)"]
        for _persp, _out in zip(("metrics", "logs", "traces"), specialist_outputs, strict=True):
            if _out.hypotheses:
                parts.append(f"\n### {_persp} specialist:\n{_serialize_partial_output(_out)}")
        prompt_text = "\n".join(parts)

    reasoning_context = _diagnosis_reasoning_context(state, deps)
    thinking = should_use_diagnosis_reasoning(
        deps.settings,
        _NODE_NAME,
        state,
        **reasoning_context,
    )
    profile_options = (
        llm_profile_call_options(
            deps.settings,
            DIAGNOSE_REASONING_PROFILE,
            aliases=(_NODE_NAME,),
        )
        if thinking
        else {}
    )

    _clear_llm_metadata(deps.llm)
    try:
        raw_output = deps.llm.generate_json(
            prompt_text,
            CompactDiagnosisOutput,
            thinking=thinking,
            **profile_options,
        )
        output = diagnosis_output_from_compact(raw_output)
    except Exception:
        agent_metrics.AgentMetricsCollector.record_llm_json_repair_attempt(
            node=_NODE_NAME,
        )
        try:
            repair_prompt = (
                "Return only valid compact diagnosis JSON. "
                "Preserve evidence_id references from the original prompt.\n\n"
                f"Original prompt:\n{prompt_text}"
            )
            raw = deps.llm.invoke(
                [{"role": "user", "content": repair_prompt}],
                thinking=False,
            )
            data = extract_json(raw)
            output = diagnosis_output_from_compact(data)
        except Exception:
            agent_metrics.AgentMetricsCollector.record_llm_fallback(
                node=_NODE_NAME,
                reason="json_repair_failed",
            )
            output = _rules_diagnosis(
                state.get("alert_name", ""), _state_evidence_ids(state)
            )
    return output  # type: ignore[no-any-return]


def _diagnosis_reasoning_context(
    state: IncidentState,
    deps: AgentDeps,
) -> dict[str, dict[str, Any]]:
    try:
        cross_validation = cross_validate_state(state)
    except Exception:
        cross_validation = {}
    try:
        cascade_analysis = analyze_cascade_from_state(
            state, topology_path=deps.settings.service_topology_path
        )
    except Exception:
        cascade_analysis = {}
    return {
        "cross_validation": cross_validation,
        "cascade_analysis": cascade_analysis,
    }


# ---------------------------------------------------------------------------
# Shared helpers (unchanged from original)
# ---------------------------------------------------------------------------


def _build_rationale(
    output: DiagnosisOutput, hypotheses: list[dict[str, object]]
) -> dict[str, object]:
    root_cause = output.root_cause
    root_evidence = root_cause.get("evidence_ids") or output.evidence_ids
    return {
        "root_cause": root_cause.get("summary", ""),
        "root_cause_confidence": root_cause.get("confidence", 0),
        "evidence_ids": list(root_evidence),
        "runbook_chunk_ids": list(output.runbook_chunk_ids),
        "hypothesis_ranking": [
            {
                "id": h.get("id", ""),
                "confidence": h.get("confidence", 0),
                "why": h.get("rank_explanation", ""),
                "evidence_ids": h.get("supporting_evidence_ids", []),
                "runbook_chunk_ids": h.get("runbook_chunk_ids", []),
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
        "k8s_evidence",
        "db_evidence",
        "verify_evidence",
        "runbook_context",
    ):
        items = state.get(key, []) or []
        for item in items if isinstance(items, list) else []:
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
    from packages.agent.rules_fallback import _DIAGNOSIS_MAP

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
