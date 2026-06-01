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
    runbook_context: list[dict[str, Any]]
    memory_context: list[dict[str, Any]]
    hypotheses: list[dict[str, Any]]
    root_cause: dict[str, Any]
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
    _built_messages: list[dict[str, Any]]
    _interrupts_enabled: bool
