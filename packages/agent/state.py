"""LangGraph IncidentState TypedDict."""

from __future__ import annotations

from typing import Any, TypedDict


class IncidentState(TypedDict, total=False):
    """State that flows through the LangGraph diagnosis workflow."""

    incident_id: str
    agent_run_id: str
    alert_payload: dict[str, Any]
    service_name: str
    severity: str
    alert_name: str
    time_window: dict[str, Any]
    metrics_evidence: list[dict[str, Any]]
    logs_evidence: list[dict[str, Any]]
    traces_evidence: list[dict[str, Any]]
    deployment_evidence: list[dict[str, Any]]
    # Phase 2.2/2.3 read-only diagnosis evidence (empty when those tools are
    # not provided, e.g. in the eval harness).
    k8s_evidence: list[dict[str, Any]]
    db_evidence: list[dict[str, Any]]
    runbook_context: list[dict[str, Any]]
    memory_context: list[dict[str, Any]]
    cross_incident_context: list[dict[str, Any]]
    hypotheses: list[dict[str, Any]]
    root_cause: dict[str, Any]
    diagnosis_rationale: dict[str, Any]
    llm_calls: list[dict[str, Any]]
    cross_validation: dict[str, Any]
    needs_human_review: bool
    cascade_analysis: dict[str, Any]
    recommended_actions: list[dict[str, Any]]
    approval_status: dict[str, Any]
    execution_results: list[dict[str, Any]]
    incident_report: dict[str, Any]
    token_budget: dict[str, Any]
    compression_events: list[dict[str, Any]]
    errors: list[dict[str, Any]]
    phase: str
    _needs_approval: bool
    _all_l4: bool
    approval_decision: str
    rejection_feedback: str
    _replan_count: int
    # ReAct micro-loop state
    verify_result: str
    verify_evidence: list[dict[str, Any]]
    verify_gates: list[dict[str, Any]]
    _verify_cycles: int
    _collect_gap_cycles: int
    # Snapshot + rollback (Phase 2.5)
    pre_action_snapshot: dict[str, Any]
    _built_messages: list[dict[str, Any]]
    _interrupts_enabled: bool
