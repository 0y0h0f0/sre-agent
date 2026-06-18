from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from packages.common.time import utc_now
from packages.db.models import (
    Action,
    AgentRun,
    AgentRunNode,
    Approval,
    EvalRun,
    EvidenceItem,
    Incident,
    IncidentReport,
    ToolCall,
)


def test_engineering_metrics_empty_database(client: TestClient) -> None:
    response = client.get("/api/evals/engineering-metrics")

    assert response.status_code == 200
    payload = response.json()
    assert payload["latest_smoke_eval_run_id"] is None
    assert payload["summary"]["agent_runs"] == {
        "total": 0,
        "terminal": 0,
        "succeeded": 0,
    }

    metrics = _metrics_by_key(payload)
    assert metrics["agent_run_success_rate"]["status"] == "unknown"
    assert metrics["unapproved_high_risk_execution_count"]["value"] == 0
    assert metrics["unapproved_high_risk_execution_count"]["status"] == "pass"
    assert metrics["unapproved_high_risk_execution_count"]["score"] == 100.0
    assert metrics["unapproved_high_risk_execution_count"]["reproduction"]
    assert metrics["backend_test_coverage"]["status"] == "unknown"
    assert metrics["backend_test_coverage"]["score"] is None
    assert "pytest tests/unit tests/integration" in metrics["backend_test_coverage"][
        "reproduction"
    ][0]

    scorecard = payload["scorecard"]
    assert scorecard["gate_status"] == "warn"
    assert scorecard["overall_score"] == 100.0
    assert scorecard["scored_metric_count"] > 0
    assert scorecard["unknown_metric_count"] > 0
    assert scorecard["completeness_rate"] < 1.0
    assert scorecard["reproduction"]


def test_engineering_metrics_aggregate_runtime_and_eval_records(
    client: TestClient,
    db_session: Session,
) -> None:
    now = utc_now()
    incident = Incident(
        incident_id="inc_metrics",
        fingerprint="fp-metrics",
        source="mock",
        service="checkout-api",
        severity="P2",
        alert_name="High5xxAfterDeploy",
        status="resolved",
        starts_at=datetime(2026, 6, 17, 0, 0, tzinfo=UTC),
        labels={},
        annotations={},
        raw_payload={},
        root_cause_summary="deployment caused 5xx",
    )
    run = AgentRun(
        agent_run_id="run_metrics",
        incident_id="inc_metrics",
        status="succeeded",
        started_at=now,
        finished_at=now,
        duration_ms=1200,
        model_name="fake-diagnosis-model",
        prompt_version="v1",
        state={
            "root_cause": {
                "summary": "deployment caused 5xx",
                "evidence_ids": ["evd_metrics"],
            }
        },
        checkpoint_thread_id="run_metrics",
        checkpoint_ns="",
        latest_checkpoint_id="chk_metrics",
        provider_cache_hit_count=3,
        provider_cache_miss_count=1,
        app_cache_hit_count=7,
        app_cache_miss_count=3,
    )
    tool_ok = ToolCall(
        tool_call_id="tool_metrics_ok",
        agent_run_id="run_metrics",
        node_name="collect_metrics",
        tool_name="metrics",
        input_json={},
        input_summary="metrics query",
        output_json={},
        output_summary="ok",
        status="succeeded",
        duration_ms=12,
        cache_key="metrics-key",
        cache_hit=True,
    )
    tool_degraded = ToolCall(
        tool_call_id="tool_metrics_degraded",
        agent_run_id="run_metrics",
        node_name="collect_logs",
        tool_name="logs",
        input_json={},
        input_summary="logs query",
        output_json={},
        output_summary="degraded",
        status="degraded",
        duration_ms=200,
        cache_key="logs-key",
        cache_hit=False,
    )
    node = AgentRunNode(
        node_id="nd_metrics",
        agent_run_id="run_metrics",
        name="diagnose",
        status="succeeded",
        started_at=now,
        finished_at=now,
        duration_ms=100,
        input_summary="context",
        output_summary="root cause",
    )
    evidence = EvidenceItem(
        evidence_id="evd_metrics",
        incident_id="inc_metrics",
        agent_run_id="run_metrics",
        type="metric",
        source="prometheus-fixture",
        title="5xx spike",
        excerpt="5xx increased after deploy",
        payload={},
    )
    report = IncidentReport(
        report_id="rpt_metrics",
        incident_id="inc_metrics",
        agent_run_id="run_metrics",
        version=1,
        root_cause="deployment caused 5xx",
        impact="checkout errors",
        timeline=[],
        actions=[],
        follow_ups=[],
        body_markdown="# Report",
    )
    action_l2 = Action(
        action_id="act_metrics_l2",
        incident_id="inc_metrics",
        agent_run_id="run_metrics",
        type="restart_service",
        risk_level="L2",
        status="succeeded",
        executor="fixture",
        target="checkout-api",
        params={},
        reason="restart after approval",
    )
    approval_l2 = Approval(
        approval_id="apv_metrics_l2",
        action_id="act_metrics_l2",
        incident_id="inc_metrics",
        agent_run_id="run_metrics",
        status="approved",
        approver="alice",
        decided_at=now,
    )
    action_l3 = Action(
        action_id="act_metrics_l3",
        incident_id="inc_metrics",
        agent_run_id="run_metrics",
        type="rollback_release",
        risk_level="L3",
        status="succeeded",
        executor="fixture",
        target="checkout-api",
        params={},
        reason="rollback after approval",
    )
    approval_l3 = Approval(
        approval_id="apv_metrics_l3",
        action_id="act_metrics_l3",
        incident_id="inc_metrics",
        agent_run_id="run_metrics",
        status="approved",
        approver="alice",
        risk_ack=True,
        confirm_action_type="rollback_release",
        confirm_target="checkout-api",
        decided_at=now,
    )
    action_l4 = Action(
        action_id="act_metrics_l4",
        incident_id="inc_metrics",
        agent_run_id="run_metrics",
        type="flush_cache",
        risk_level="L4",
        status="blocked",
        executor="fixture",
        target="redis",
        params={},
        reason="destructive action rejected",
    )
    eval_run = EvalRun(
        eval_run_id="eval_metrics_smoke",
        status="succeeded",
        suite="smoke",
        model_name="fake-diagnosis-model",
        prompt_version="v1",
        metrics={
            "case_count": 4,
            "root_cause_top1_hit_rate": 1.0,
            "root_cause_top3_hit_rate": 1.0,
            "required_evidence_coverage": 1.0,
            "high_risk_interception_rate": 1.0,
            "json_valid_rate": 1.0,
            "report_generation_rate": 1.0,
            "tool_success_rate": 0.75,
            "avg_duration_ms": 7600,
            "p95_prompt_token_estimate": 2300,
            "tool_cache_hit_rate": 0.1875,
            "compression_retention_rate": 1.0,
            "memory_misuse_rate": 0.0,
        },
        started_at=now,
        finished_at=now,
        git_commit="test",
    )
    db_session.add_all(
        [
            incident,
            run,
            tool_ok,
            tool_degraded,
            node,
            evidence,
            report,
            action_l2,
            approval_l2,
            action_l3,
            approval_l3,
            action_l4,
            eval_run,
        ]
    )
    db_session.commit()

    response = client.get("/api/evals/engineering-metrics?window_days=7")

    assert response.status_code == 200
    payload = response.json()
    assert payload["latest_smoke_eval_run_id"] == "eval_metrics_smoke"
    assert payload["summary"]["agent_runs"]["succeeded"] == 1
    assert payload["summary"]["incidents"] == {
        "total": 1,
        "open": 0,
        "active": 0,
    }
    assert payload["summary"]["tool_calls"] == {
        "total": 2,
        "succeeded": 1,
        "degraded": 1,
    }

    metrics = _metrics_by_key(payload)
    assert metrics["agent_run_success_rate"]["value"] == 1.0
    assert metrics["runtime_report_generation_rate"]["value"] == 1.0
    assert metrics["evidence_traceability_rate"]["value"] == 1.0
    assert metrics["evidence_record_completeness_rate"]["value"] == 1.0
    assert metrics["provider_prompt_cache_hit_rate"]["value"] == 0.75
    assert metrics["app_prompt_segment_cache_hit_rate"]["value"] == 0.7
    assert metrics["runtime_tool_success_rate"]["value"] == 0.5
    assert metrics["runtime_tool_success_rate"]["score"] == 55.6
    assert metrics["runtime_tool_degraded_rate"]["value"] == 0.5
    assert metrics["runtime_tool_degraded_rate"]["score"] == 20.0
    assert metrics["runtime_tool_cache_hit_rate"]["value"] == 0.5
    assert metrics["runtime_tool_cache_hit_rate"]["score"] == 100.0
    assert metrics["tool_call_coverage_rate"]["value"] == 1.0
    assert metrics["diagnosis_duration_p95_ms"]["value"] == 1200
    assert metrics["diagnosis_duration_p95_ms"]["score"] == 100.0
    assert metrics["tool_call_duration_p95_ms"]["value"] == 200
    assert metrics["agent_node_duration_p95_ms"]["value"] == 100
    assert metrics["unapproved_high_risk_execution_count"]["value"] == 0
    assert metrics["l4_approval_count"]["value"] == 0
    assert metrics["l4_not_blocked_count"]["value"] == 0
    assert metrics["active_incident_backlog_count"]["value"] == 0
    assert metrics["incident_resolution_rate"]["value"] == 1.0
    assert metrics["waiting_approval_backlog_count"]["value"] == 0
    assert metrics["approval_decision_rate"]["value"] == 1.0
    assert metrics["l2_l3_approval_coverage_rate"]["value"] == 1.0
    assert metrics["l3_confirmation_valid_rate"]["value"] == 1.0
    assert metrics["executed_action_success_rate"]["value"] == 1.0
    assert metrics["agent_node_success_rate"]["value"] == 1.0
    assert metrics["failed_agent_node_count"]["value"] == 0
    assert metrics["checkpoint_pointer_coverage_rate"]["value"] == 1.0
    assert metrics["agent_node_trace_coverage_rate"]["value"] == 1.0
    assert metrics["report_section_completeness_rate"]["value"] == 1.0
    assert metrics["report_version_issue_count"]["value"] == 0
    assert metrics["root_cause_top1_hit_rate"]["value"] == 1.0
    assert metrics["eval_avg_duration_ms"]["value"] == 7600
    assert metrics["eval_p95_prompt_token_estimate"]["value"] == 2300
    assert metrics["eval_tool_cache_hit_rate"]["value"] == 0.1875
    assert metrics["eval_compression_retention_rate"]["value"] == 1.0
    assert metrics["high_risk_interception_rate"]["status"] == "pass"
    assert metrics["high_risk_interception_rate"]["score"] == 100.0
    assert metrics["high_risk_interception_rate"]["reproduction"]
    assert metrics["ruff_lint_status"]["status"] == "unknown"
    assert metrics["mypy_type_check_status"]["status"] == "unknown"
    assert metrics["dependency_vulnerability_status"]["status"] == "unknown"
    assert metrics["api_contract_test_status"]["status"] == "unknown"

    scorecard = payload["scorecard"]
    assert scorecard["overall_score"] > 90.0
    assert scorecard["gate_status"] == "warn"
    assert scorecard["fail_count"] == 2
    assert scorecard["completeness_rate"] < 1.0
    categories = {item["category"]: item for item in scorecard["category_scores"]}
    assert categories["quality"]["score"] == 100.0
    assert categories["reliability"]["status"] == "fail"
    assert categories["maintainability"]["status"] == "unknown"
    assert any("Runtime tool success rate" in risk for risk in scorecard["top_risks"])


def _metrics_by_key(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["key"]: item for item in payload["metrics"]}
