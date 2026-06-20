"""LangGraph IncidentState TypedDict.

This state is the in-memory handoff object between graph nodes. Persisted data
still lives in the database repositories and LangGraph checkpoint tables; this
TypedDict is a typed view of what the next node needs to continue safely.
"""

from __future__ import annotations

from typing import Any, TypedDict


class IncidentState(TypedDict, total=False):
    """State that flows through the LangGraph diagnosis workflow.

    The shape is intentionally permissive (``total=False``) because a node only
    owns the fields it has produced so far. Required business invariants are
    enforced at node/service boundaries rather than by assuming every field is
    present from the first step.
    """

    # Stable business identifiers. ``agent_run_id`` is also the LangGraph
    # checkpointer thread_id, so approval resume must keep the same value.
    incident_id: str
    agent_run_id: str

    # Normalized alert context produced by parse_alert. Downstream tool queries
    # should read these fields instead of re-parsing provider-specific payloads.
    alert_payload: dict[str, Any]
    service_name: str
    severity: str
    alert_name: str
    time_window: dict[str, Any]

    # Primary evidence buckets. Items are compact dictionaries that may receive
    # persisted ``evidence_id`` values after repository writes; large raw logs
    # should be compressed before they become prompt context.
    metrics_evidence: list[dict[str, Any]]
    logs_evidence: list[dict[str, Any]]
    traces_evidence: list[dict[str, Any]]
    deployment_evidence: list[dict[str, Any]]
    # Phase 2.2/2.3 read-only diagnosis evidence (empty when those tools are
    # not provided, e.g. in the eval harness).
    k8s_evidence: list[dict[str, Any]]
    db_evidence: list[dict[str, Any]]

    # Retrieval context. Runbook items should retain chunk/source metadata so
    # diagnosis, reports, and audits can point back to their supporting text.
    runbook_context: list[dict[str, Any]]
    memory_context: list[dict[str, Any]]
    cross_incident_context: list[dict[str, Any]]

    # Diagnosis output. ``evidence_ids`` and ``runbook_chunk_ids`` are part of
    # the audit trail: root-cause statements should remain traceable after
    # compression, report generation, and memory persistence.
    hypotheses: list[dict[str, Any]]
    root_cause: dict[str, Any]
    evidence_ids: list[str]
    runbook_chunk_ids: list[str]
    diagnosis_rationale: dict[str, Any]
    llm_calls: list[dict[str, Any]]
    cross_validation: dict[str, Any]
    needs_human_review: bool
    cascade_analysis: dict[str, Any]

    # Planner and guardrail output. The planner may suggest actions, but only
    # guardrail_check is allowed to attach final risk/allowed/approval fields.
    recommended_actions: list[dict[str, Any]]
    approval_status: dict[str, Any]
    execution_results: list[dict[str, Any]]
    incident_report: dict[str, Any]

    # Context-efficiency metadata. These are reported separately from provider
    # prompt-cache information because Redis/app cache hits are not equivalent
    # to provider-side cache hits.
    token_budget: dict[str, Any]
    compression_events: list[dict[str, Any]]

    # Node-level operational state. ``phase`` is useful for routing/debugging
    # but should not be treated as a durable external API contract.
    errors: list[dict[str, Any]]
    phase: str

    # Internal route flags written by guardrail_check.
    _needs_approval: bool
    _all_l4: bool

    # Approval/replan bookkeeping. ``approval_decision`` is only a resume hint;
    # human_approval reconciles the real per-action statuses from the database.
    approval_decision: str
    rejection_feedback: str
    _replan_count: int

    # ReAct micro-loop state
    verify_result: str
    verify_evidence: list[dict[str, Any]]
    verify_gates: list[dict[str, Any]]
    _verify_cycles: int
    _collect_gap_cycles: int

    # Snapshot + rollback state. The snapshot is intentionally captured before
    # execution so degraded replans can reason from known pre-action values.
    pre_action_snapshot: dict[str, Any]

    # Private prompt/context fields. These are for graph internals and should
    # not be exposed as API response fields without explicit sanitization.
    _built_messages: list[dict[str, Any]]
    _interrupts_enabled: bool
