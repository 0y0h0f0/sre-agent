"""Project-level engineering metrics aggregation."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import timedelta
from math import ceil
from typing import Any, Literal

from sqlalchemy.orm import Session

from apps.api.schemas.evals import (
    EngineeringCategoryScore,
    EngineeringMetric,
    EngineeringMetricsResponse,
    EngineeringScorecard,
)
from packages.common.time import utc_now
from packages.db.models import Action, AgentRun, AgentRunNode, Approval, Incident, IncidentReport
from packages.db.repositories.engineering_metrics import EngineeringMetricsRepository

_TERMINAL_RUN_STATUSES = {"succeeded", "failed", "cancelled"}
_EXECUTION_STATUSES = {"executing", "succeeded", "failed"}
_SAFE_LOCAL_EXECUTORS = {"fixture", "mock"}
_CATEGORY_WEIGHTS = {
    "safety": 0.25,
    "quality": 0.20,
    "reliability": 0.20,
    "performance": 0.10,
    "maintainability": 0.10,
    "delivery": 0.10,
    "efficiency": 0.05,
}
_HARD_GATE_KEYS = {
    "high_risk_interception_rate",
    "unapproved_high_risk_execution_count",
    "l3_approval_missing_confirmation_count",
    "l4_approval_count",
    "l4_not_blocked_count",
}
_SOURCE_REPRODUCTION = {
    "latest_smoke_eval": [
        "python -m packages.evals.runner --suite smoke --output reports/eval-smoke.json",
        "curl http://localhost:8000/api/evals/runs",
    ],
    "database": [
        "curl http://localhost:8000/api/evals/engineering-metrics?window_days=30",
    ],
    "ci_coverage": [
        (
            "pytest tests/unit tests/integration --cov=apps --cov=packages "
            "--cov-report=term-missing --cov-report=xml --cov-fail-under=80"
        ),
    ],
    "frontend_ci": [
        "cd apps/web && npm run test:coverage",
    ],
    "ci": [
        (
            "Inspect the CI workflow run for ruff, mypy, pytest, smoke eval, "
            "Vitest, build, and Playwright status."
        ),
    ],
    "static_analysis": [
        "ruff check apps packages tests",
        "mypy apps packages",
    ],
    "security_scan": [
        "python -m pip-audit",
        "cd apps/web && npm audit --audit-level=high",
    ],
    "contract_tests": [
        "pytest tests/contract",
    ],
    "prometheus": [
        "curl http://localhost:8000/metrics",
        "Query Prometheus for HTTP request duration histogram p95 by route.",
    ],
    "vcs_ci_cd": [
        "Export deployment and PR timestamps from the CI/CD and VCS provider.",
    ],
    "incident_process": [
        "Export production incident opened/resolved timestamps from the incident system.",
    ],
}
MetricStatus = Literal["pass", "fail", "warn", "unknown"]


class EngineeringMetricsService:
    """Build a read-only project evaluation snapshot from existing records."""

    def __init__(self, db: Session) -> None:
        self._db = db
        self._repo = EngineeringMetricsRepository(db)

    def get_summary(self, *, window_days: int = 30) -> EngineeringMetricsResponse:
        generated_at = utc_now()
        window_started_at = generated_at - timedelta(days=window_days)
        incidents = self._repo.list_incidents(since=window_started_at)
        runs = self._repo.list_agent_runs(since=window_started_at)
        tool_calls = self._repo.list_tool_calls(since=window_started_at)
        nodes = self._repo.list_agent_run_nodes(since=window_started_at)
        actions = self._repo.list_actions(since=window_started_at)
        approvals = self._repo.list_approvals(since=window_started_at)
        reports = self._repo.list_reports(since=window_started_at)
        evidence_items = self._repo.list_evidence_items(since=window_started_at)
        latest_smoke_eval = self._repo.latest_eval_run(
            suite="smoke", since=window_started_at
        )

        terminal_runs = [run for run in runs if run.status in _TERMINAL_RUN_STATUSES]
        succeeded_runs = [run for run in runs if run.status == "succeeded"]
        tool_total = len(tool_calls)
        action_by_id = {action.action_id: action for action in actions}
        approved_action_ids = {
            approval.action_id for approval in approvals if approval.status == "approved"
        }
        reports_by_run = {report.agent_run_id for report in reports}
        evidence_by_run = {item.agent_run_id for item in evidence_items}
        evidence_ids_by_run = _evidence_ids_by_run(evidence_items)
        latest_eval_metrics = latest_smoke_eval.metrics if latest_smoke_eval else {}

        metrics: list[EngineeringMetric] = []
        metrics.extend(self._eval_metrics(latest_eval_metrics, bool(latest_smoke_eval)))
        metrics.extend(
            self._runtime_quality_metrics(
                runs=runs,
                terminal_runs=terminal_runs,
                succeeded_runs=succeeded_runs,
                reports_by_run=reports_by_run,
                evidence_by_run=evidence_by_run,
                evidence_ids_by_run=evidence_ids_by_run,
            )
        )
        metrics.extend(self._incident_backlog_metrics(incidents=incidents))
        metrics.extend(
            self._safety_metrics(
                actions=actions,
                approvals=approvals,
                action_by_id=action_by_id,
                approved_action_ids=approved_action_ids,
            )
        )
        metrics.extend(
            self._tool_and_performance_metrics(
                runs=runs,
                tool_calls=tool_calls,
                nodes=nodes,
                tool_total=tool_total,
            )
        )
        metrics.extend(
            self._workflow_integrity_metrics(
                runs=runs,
                nodes=nodes,
                actions=actions,
                approvals=approvals,
                reports=reports,
            )
        )
        metrics.extend(self._external_metrics())
        scorecard = _build_scorecard(metrics)

        return EngineeringMetricsResponse(
            generated_at=generated_at,
            window_days=window_days,
            window_started_at=window_started_at,
            latest_smoke_eval_run_id=latest_smoke_eval.eval_run_id
            if latest_smoke_eval
            else None,
            summary={
                "agent_runs": {
                    "total": len(runs),
                    "terminal": len(terminal_runs),
                    "succeeded": len(succeeded_runs),
                },
                "incidents": {
                    "total": len(incidents),
                    "open": sum(1 for incident in incidents if incident.status == "open"),
                    "active": sum(
                        1
                        for incident in incidents
                        if incident.status in {"open", "diagnosing", "waiting_approval"}
                    ),
                },
                "tool_calls": {
                    "total": tool_total,
                    "succeeded": sum(
                        1 for call in tool_calls if call.status == "succeeded"
                    ),
                    "degraded": sum(
                        1 for call in tool_calls if call.status == "degraded"
                    ),
                },
                "actions": {
                    "total": len(actions),
                    "l2_l3": sum(
                        1 for action in actions if action.risk_level in {"L2", "L3"}
                    ),
                    "l4": sum(1 for action in actions if action.risk_level == "L4"),
                },
                "approvals": {
                    "total": len(approvals),
                    "approved": sum(
                        1 for approval in approvals if approval.status == "approved"
                    ),
                    "waiting": sum(
                        1 for approval in approvals if approval.status == "waiting"
                    ),
                },
                "latest_smoke_eval": {
                    "eval_run_id": latest_smoke_eval.eval_run_id
                    if latest_smoke_eval
                    else None,
                    "metric_count": len(latest_eval_metrics),
                },
            },
            scorecard=scorecard,
            metrics=metrics,
        )

    def _eval_metrics(
        self, latest_eval_metrics: dict[str, Any], has_eval: bool
    ) -> list[EngineeringMetric]:
        def value(key: str) -> Any | None:
            if not has_eval:
                return None
            return latest_eval_metrics.get(key)

        return [
            _metric(
                key="smoke_eval_case_count",
                category="quality",
                label="Smoke eval case count",
                value=value("case_count"),
                unit="cases",
                target=">= 4",
                status=_status_min(value("case_count"), 4),
                score=_score_min(value("case_count"), 4),
                source="latest_smoke_eval",
                description=(
                    "Number of deterministic FakeLLM smoke cases in the latest "
                    "succeeded smoke eval."
                ),
            ),
            _metric(
                key="root_cause_top1_hit_rate",
                category="quality",
                label="Root cause Top-1 hit rate",
                value=value("root_cause_top1_hit_rate"),
                unit="ratio",
                target=">= 1.0 for smoke CI gate",
                status=_status_min(value("root_cause_top1_hit_rate"), 1.0),
                score=_score_min(value("root_cause_top1_hit_rate"), 1.0),
                source="latest_smoke_eval",
                description="Share of smoke cases whose top root cause matches expected keywords.",
            ),
            _metric(
                key="root_cause_top3_hit_rate",
                category="quality",
                label="Root cause Top-3 hit rate",
                value=value("root_cause_top3_hit_rate"),
                unit="ratio",
                target=">= 1.0 for smoke CI gate",
                status=_status_min(value("root_cause_top3_hit_rate"), 1.0),
                score=_score_min(value("root_cause_top3_hit_rate"), 1.0),
                source="latest_smoke_eval",
                description=(
                    "Share of smoke cases whose ranked hypotheses include the "
                    "expected cause."
                ),
            ),
            _metric(
                key="required_evidence_coverage",
                category="quality",
                label="Required evidence coverage",
                value=value("required_evidence_coverage"),
                unit="ratio",
                target=">= 1.0 for smoke CI gate",
                status=_status_min(value("required_evidence_coverage"), 1.0),
                score=_score_min(value("required_evidence_coverage"), 1.0),
                source="latest_smoke_eval",
                description="Share of smoke cases with the required evidence types present.",
            ),
            _metric(
                key="high_risk_interception_rate",
                category="safety",
                label="High-risk interception rate",
                value=value("high_risk_interception_rate"),
                unit="ratio",
                target="1.0",
                status=_status_min(value("high_risk_interception_rate"), 1.0),
                score=_score_min(value("high_risk_interception_rate"), 1.0),
                source="latest_smoke_eval",
                description=(
                    "Share of expected L2/L3 smoke cases that entered approval "
                    "instead of executing directly."
                ),
            ),
            _metric(
                key="json_valid_rate",
                category="quality",
                label="Structured JSON validity",
                value=value("json_valid_rate"),
                unit="ratio",
                target="1.0",
                status=_status_min(value("json_valid_rate"), 1.0),
                score=_score_min(value("json_valid_rate"), 1.0),
                source="latest_smoke_eval",
                description=(
                    "Share of smoke cases with valid root cause, hypotheses, "
                    "and action structures."
                ),
            ),
            _metric(
                key="smoke_eval_report_generation_rate",
                category="quality",
                label="Smoke report generation rate",
                value=value("report_generation_rate"),
                unit="ratio",
                target="1.0",
                status=_status_min(value("report_generation_rate"), 1.0),
                score=_score_min(value("report_generation_rate"), 1.0),
                source="latest_smoke_eval",
                description="Share of smoke cases that generated an incident report.",
            ),
            _metric(
                key="eval_tool_success_rate",
                category="reliability",
                label="Eval tool success rate",
                value=value("tool_success_rate"),
                unit="ratio",
                target=">= 0.75",
                status=_status_min(value("tool_success_rate"), 0.75),
                score=_score_min(value("tool_success_rate"), 0.75),
                source="latest_smoke_eval",
                description="Tool success rate observed by the deterministic eval harness.",
            ),
            _metric(
                key="eval_avg_duration_ms",
                category="performance",
                label="Eval average case duration",
                value=value("avg_duration_ms"),
                unit="ms",
                target="<= 15000 for smoke eval",
                status=_status_max(value("avg_duration_ms"), 15000),
                score=_score_max(value("avg_duration_ms"), 15000),
                source="latest_smoke_eval",
                description="Average smoke eval case runtime from the latest succeeded run.",
            ),
            _metric(
                key="eval_p95_prompt_token_estimate",
                category="efficiency",
                label="Eval prompt token estimate P95",
                value=value("p95_prompt_token_estimate"),
                unit="tokens",
                target="<= 3000 for smoke eval",
                status=_status_max(value("p95_prompt_token_estimate"), 3000),
                score=_score_max(value("p95_prompt_token_estimate"), 3000),
                source="latest_smoke_eval",
                description=(
                    "P95 estimated prompt tokens per smoke eval case; tracks "
                    "context growth."
                ),
            ),
            _metric(
                key="eval_tool_cache_hit_rate",
                category="efficiency",
                label="Eval tool cache hit rate",
                value=value("tool_cache_hit_rate"),
                unit="ratio",
                target=">= 0.10 for deterministic smoke eval",
                status=_status_min(value("tool_cache_hit_rate"), 0.10, fail="warn"),
                score=_score_min(value("tool_cache_hit_rate"), 0.10),
                source="latest_smoke_eval",
                description="Tool cache hit ratio observed by the deterministic eval harness.",
            ),
            _metric(
                key="eval_compression_retention_rate",
                category="efficiency",
                label="Eval compression retention rate",
                value=value("compression_retention_rate"),
                unit="ratio",
                target="<= 1.0",
                status=_status_max(value("compression_retention_rate"), 1.0, fail="warn"),
                score=_score_max(value("compression_retention_rate"), 1.0),
                source="latest_smoke_eval",
                description=(
                    "Compressed context token estimate divided by pre-compression "
                    "tokens; values above 1 indicate expansion."
                ),
            ),
            _metric(
                key="eval_memory_misuse_rate",
                category="quality",
                label="Eval memory misuse rate",
                value=value("memory_misuse_rate"),
                unit="ratio",
                target="<= 0.05",
                status=_status_max(value("memory_misuse_rate"), 0.05),
                score=_score_max(value("memory_misuse_rate"), 0.05),
                source="latest_smoke_eval",
                description="Share of eval cases where unrelated memory influenced the run.",
            ),
        ]

    def _runtime_quality_metrics(
        self,
        *,
        runs: list[AgentRun],
        terminal_runs: list[AgentRun],
        succeeded_runs: list[AgentRun],
        reports_by_run: set[str],
        evidence_by_run: set[str],
        evidence_ids_by_run: dict[str, set[str]],
    ) -> list[EngineeringMetric]:
        run_success_rate = _rate(len(succeeded_runs), len(terminal_runs))
        report_generation_rate = _rate(
            sum(1 for run in succeeded_runs if run.agent_run_id in reports_by_run),
            len(succeeded_runs),
        )
        traceable_runs = sum(
            1 for run in succeeded_runs if _run_has_traceable_evidence(run, evidence_by_run)
        )
        evidence_traceability_rate = _rate(traceable_runs, len(succeeded_runs))
        evidence_record_completeness_rate = _rate(
            sum(
                1
                for run in succeeded_runs
                if _run_has_complete_evidence_records(run, evidence_ids_by_run)
            ),
            len(succeeded_runs),
        )
        provider_cache_rate = _rate(
            sum(run.provider_cache_hit_count for run in runs),
            sum(run.provider_cache_hit_count + run.provider_cache_miss_count for run in runs),
        )
        app_cache_rate = _rate(
            sum(run.app_cache_hit_count for run in runs),
            sum(run.app_cache_hit_count + run.app_cache_miss_count for run in runs),
        )

        return [
            _metric(
                key="agent_run_success_rate",
                category="reliability",
                label="Agent run success rate",
                value=run_success_rate,
                unit="ratio",
                target=">= 0.95",
                status=_status_min(run_success_rate, 0.95),
                score=_score_min(run_success_rate, 0.95),
                source="database",
                description=(
                    "Succeeded terminal agent runs divided by all terminal agent "
                    "runs in the window."
                ),
            ),
            _metric(
                key="runtime_report_generation_rate",
                category="reliability",
                label="Runtime report generation rate",
                value=report_generation_rate,
                unit="ratio",
                target=">= 0.95",
                status=_status_min(report_generation_rate, 0.95),
                score=_score_min(report_generation_rate, 0.95),
                source="database",
                description="Succeeded agent runs with a persisted incident report.",
            ),
            _metric(
                key="evidence_traceability_rate",
                category="quality",
                label="Evidence traceability rate",
                value=evidence_traceability_rate,
                unit="ratio",
                target=">= 0.95",
                status=_status_min(evidence_traceability_rate, 0.95),
                score=_score_min(evidence_traceability_rate, 0.95),
                source="database",
                description=(
                    "Succeeded runs whose root cause carries evidence IDs or has "
                    "persisted evidence records."
                ),
            ),
            _metric(
                key="evidence_record_completeness_rate",
                category="quality",
                label="Evidence record completeness",
                value=evidence_record_completeness_rate,
                unit="ratio",
                target=">= 0.95",
                status=_status_min(evidence_record_completeness_rate, 0.95),
                score=_score_min(evidence_record_completeness_rate, 0.95),
                source="database",
                description=(
                    "Succeeded runs whose evidence references are traceable to "
                    "persisted evidence records."
                ),
            ),
            _metric(
                key="provider_prompt_cache_hit_rate",
                category="efficiency",
                label="Provider prompt cache hit rate",
                value=provider_cache_rate,
                unit="ratio",
                target=">= 0.60 when provider reports cache usage",
                status=_status_min(provider_cache_rate, 0.60, missing="unknown", fail="warn"),
                score=_score_min(provider_cache_rate, 0.60),
                source="database",
                description=(
                    "Provider cache hits divided by provider cache hits plus misses; "
                    "unknown when provider data is unavailable."
                ),
            ),
            _metric(
                key="app_prompt_segment_cache_hit_rate",
                category="efficiency",
                label="App prompt segment cache hit rate",
                value=app_cache_rate,
                unit="ratio",
                target=">= 0.70",
                status=_status_min(app_cache_rate, 0.70, missing="unknown", fail="warn"),
                score=_score_min(app_cache_rate, 0.70),
                source="database",
                description=(
                    "Application prompt segment cache hits divided by app cache "
                    "hits plus misses."
                ),
            ),
        ]

    def _incident_backlog_metrics(self, *, incidents: list[Incident]) -> list[EngineeringMetric]:
        active_statuses = {"open", "diagnosing", "waiting_approval"}
        active_count = sum(1 for incident in incidents if incident.status in active_statuses)
        open_count = sum(1 for incident in incidents if incident.status == "open")
        resolved_count = sum(
            1 for incident in incidents if incident.status in {"mitigated", "resolved"}
        )
        resolution_rate = _rate(resolved_count, len(incidents))

        return [
            _metric(
                key="active_incident_backlog_count",
                category="reliability",
                label="Active incident backlog",
                value=active_count,
                unit="count",
                target="0 in local/CI demo state",
                status=_status_zero(active_count, nonzero="warn"),
                score=_score_zero(active_count, nonzero_score=70.0),
                source="database",
                description=(
                    "Incidents still open, diagnosing, or waiting for approval in "
                    "the selected window."
                ),
            ),
            _metric(
                key="open_incident_count",
                category="reliability",
                label="Open incident count",
                value=open_count,
                unit="count",
                target="0 in local/CI demo state",
                status=_status_zero(open_count, nonzero="warn"),
                score=_score_zero(open_count, nonzero_score=70.0),
                source="database",
                description="Incidents still in the raw open status.",
            ),
            _metric(
                key="incident_resolution_rate",
                category="reliability",
                label="Incident resolution rate",
                value=resolution_rate,
                unit="ratio",
                target=">= 0.95",
                status=_status_min(resolution_rate, 0.95, fail="warn"),
                score=_score_min(resolution_rate, 0.95),
                source="database",
                description=(
                    "Mitigated or resolved incidents divided by all incidents in "
                    "the selected window."
                ),
            ),
        ]

    def _safety_metrics(
        self,
        *,
        actions: list[Action],
        approvals: list[Approval],
        action_by_id: dict[str, Action],
        approved_action_ids: set[str],
    ) -> list[EngineeringMetric]:
        unapproved_high_risk_execution_count = sum(
            1
            for action in actions
            if action.risk_level in {"L2", "L3"}
            and action.status in _EXECUTION_STATUSES
            and action.action_id not in approved_action_ids
        )
        l3_approval_missing_confirmation_count = sum(
            1
            for approval in approvals
            if approval.status == "approved"
            and _approval_action_risk(approval, action_by_id) == "L3"
            and not _l3_confirmation_valid(approval, action_by_id[approval.action_id])
        )
        l4_approval_count = sum(
            1
            for approval in approvals
            if _approval_action_risk(approval, action_by_id) == "L4"
        )
        l4_not_blocked_count = sum(
            1
            for action in actions
            if action.risk_level == "L4" and action.status not in {"blocked", "rejected"}
        )
        non_fixture_executor_action_count = sum(
            1 for action in actions if action.executor not in _SAFE_LOCAL_EXECUTORS
        )

        return [
            _metric(
                key="unapproved_high_risk_execution_count",
                category="safety",
                label="Unapproved L2/L3 executions",
                value=unapproved_high_risk_execution_count,
                unit="count",
                target="0",
                status=_status_zero(unapproved_high_risk_execution_count),
                score=_score_zero(unapproved_high_risk_execution_count),
                source="database",
                description=(
                    "L2/L3 actions that reached an execution status without an "
                    "approved approval record."
                ),
            ),
            _metric(
                key="l3_approval_missing_confirmation_count",
                category="safety",
                label="Invalid L3 approvals",
                value=l3_approval_missing_confirmation_count,
                unit="count",
                target="0",
                status=_status_zero(l3_approval_missing_confirmation_count),
                score=_score_zero(l3_approval_missing_confirmation_count),
                source="database",
                description=(
                    "Approved L3 approvals missing risk_ack or matching "
                    "confirm_action_type/confirm_target."
                ),
            ),
            _metric(
                key="l4_approval_count",
                category="safety",
                label="L4 approvals created",
                value=l4_approval_count,
                unit="count",
                target="0",
                status=_status_zero(l4_approval_count),
                score=_score_zero(l4_approval_count),
                source="database",
                description="Any approval rows associated with L4 destructive actions.",
            ),
            _metric(
                key="l4_not_blocked_count",
                category="safety",
                label="L4 actions not blocked",
                value=l4_not_blocked_count,
                unit="count",
                target="0",
                status=_status_zero(l4_not_blocked_count),
                score=_score_zero(l4_not_blocked_count),
                source="database",
                description="L4 actions whose status is not blocked or rejected.",
            ),
            _metric(
                key="non_fixture_executor_action_count",
                category="safety",
                label="Non-fixture executor actions",
                value=non_fixture_executor_action_count,
                unit="count",
                target="0 in local/CI; live requires explicit opt-in",
                status=_status_zero(non_fixture_executor_action_count, nonzero="warn"),
                score=_score_zero(non_fixture_executor_action_count, nonzero_score=70.0),
                source="database",
                description="Actions recorded with an executor outside fixture/mock defaults.",
            ),
        ]

    def _workflow_integrity_metrics(
        self,
        *,
        runs: list[AgentRun],
        nodes: list[AgentRunNode],
        actions: list[Action],
        approvals: list[Approval],
        reports: list[IncidentReport],
    ) -> list[EngineeringMetric]:
        waiting_approval_count = sum(1 for approval in approvals if approval.status == "waiting")
        decision_rate = _rate(
            sum(1 for approval in approvals if approval.status != "waiting"),
            len(approvals),
        )
        approval_action_ids = {approval.action_id for approval in approvals}
        l2_l3_actions = [action for action in actions if action.risk_level in {"L2", "L3"}]
        l2_l3_approval_coverage_rate = _rate(
            sum(1 for action in l2_l3_actions if action.action_id in approval_action_ids),
            len(l2_l3_actions),
        )
        action_by_id = {action.action_id: action for action in actions}
        approved_l3 = [
            approval
            for approval in approvals
            if approval.status == "approved"
            and _approval_action_risk(approval, action_by_id) == "L3"
        ]
        l3_confirmation_valid_rate = _rate(
            sum(
                1
                for approval in approved_l3
                if _l3_confirmation_valid(approval, action_by_id[approval.action_id])
            ),
            len(approved_l3),
        )
        executed_actions = [
            action for action in actions if action.status in {"succeeded", "failed"}
        ]
        executed_action_success_rate = _rate(
            sum(1 for action in executed_actions if action.status == "succeeded"),
            len(executed_actions),
        )
        node_success_rate = _rate(
            sum(1 for node in nodes if node.status == "succeeded"),
            len(nodes),
        )
        failed_node_count = sum(1 for node in nodes if node.status == "failed")
        checkpoint_runs = [
            run for run in runs if run.status in {*_TERMINAL_RUN_STATUSES, "waiting_approval"}
        ]
        checkpoint_pointer_coverage_rate = _rate(
            sum(1 for run in checkpoint_runs if _run_has_checkpoint_pointer(run)),
            len(checkpoint_runs),
        )
        node_run_ids = {node.agent_run_id for node in nodes}
        node_trace_coverage_rate = _rate(
            sum(1 for run in runs if run.agent_run_id in node_run_ids),
            len(runs),
        )
        report_section_completeness_rate = _rate(
            sum(1 for report in reports if _report_has_core_sections(report)),
            len(reports),
        )
        report_version_issue_count = _report_version_issue_count(reports)

        return [
            _metric(
                key="waiting_approval_backlog_count",
                category="safety",
                label="Waiting approval backlog",
                value=waiting_approval_count,
                unit="count",
                target="0 in local/CI demo state",
                status=_status_zero(waiting_approval_count, nonzero="warn"),
                score=_score_zero(waiting_approval_count, nonzero_score=70.0),
                source="database",
                description="Approval requests still waiting for an operator decision.",
            ),
            _metric(
                key="approval_decision_rate",
                category="safety",
                label="Approval decision rate",
                value=decision_rate,
                unit="ratio",
                target=">= 0.95",
                status=_status_min(decision_rate, 0.95, fail="warn"),
                score=_score_min(decision_rate, 0.95),
                source="database",
                description="Non-waiting approvals divided by all approvals in the window.",
            ),
            _metric(
                key="l2_l3_approval_coverage_rate",
                category="safety",
                label="L2/L3 approval coverage",
                value=l2_l3_approval_coverage_rate,
                unit="ratio",
                target="1.0",
                status=_status_min(l2_l3_approval_coverage_rate, 1.0),
                score=_score_min(l2_l3_approval_coverage_rate, 1.0),
                source="database",
                description="L2/L3 actions with an associated approval record.",
            ),
            _metric(
                key="l3_confirmation_valid_rate",
                category="safety",
                label="L3 confirmation validity rate",
                value=l3_confirmation_valid_rate,
                unit="ratio",
                target="1.0",
                status=_status_min(l3_confirmation_valid_rate, 1.0),
                score=_score_min(l3_confirmation_valid_rate, 1.0),
                source="database",
                description=(
                    "Approved L3 approvals whose risk_ack/action type/target "
                    "confirmation matches the action."
                ),
            ),
            _metric(
                key="executed_action_success_rate",
                category="reliability",
                label="Executed action success rate",
                value=executed_action_success_rate,
                unit="ratio",
                target=">= 0.95",
                status=_status_min(executed_action_success_rate, 0.95, fail="warn"),
                score=_score_min(executed_action_success_rate, 0.95),
                source="database",
                description="Succeeded executed actions divided by succeeded plus failed actions.",
            ),
            _metric(
                key="agent_node_success_rate",
                category="reliability",
                label="Agent node success rate",
                value=node_success_rate,
                unit="ratio",
                target=">= 0.95",
                status=_status_min(node_success_rate, 0.95),
                score=_score_min(node_success_rate, 0.95),
                source="database",
                description="Succeeded LangGraph node trace records divided by all node traces.",
            ),
            _metric(
                key="failed_agent_node_count",
                category="reliability",
                label="Failed agent node count",
                value=failed_node_count,
                unit="count",
                target="0",
                status=_status_zero(failed_node_count),
                score=_score_zero(failed_node_count),
                source="database",
                description="LangGraph node trace records with failed status.",
            ),
            _metric(
                key="checkpoint_pointer_coverage_rate",
                category="reliability",
                label="Checkpoint pointer coverage",
                value=checkpoint_pointer_coverage_rate,
                unit="ratio",
                target=">= 0.95",
                status=_status_min(checkpoint_pointer_coverage_rate, 0.95, fail="warn"),
                score=_score_min(checkpoint_pointer_coverage_rate, 0.95),
                source="database",
                description=(
                    "Terminal or waiting approval runs with persisted checkpoint "
                    "thread and latest checkpoint pointers."
                ),
            ),
            _metric(
                key="agent_node_trace_coverage_rate",
                category="reliability",
                label="Agent node trace coverage",
                value=node_trace_coverage_rate,
                unit="ratio",
                target=">= 0.95",
                status=_status_min(node_trace_coverage_rate, 0.95),
                score=_score_min(node_trace_coverage_rate, 0.95),
                source="database",
                description="Agent runs with at least one persisted LangGraph node trace.",
            ),
            _metric(
                key="report_section_completeness_rate",
                category="quality",
                label="Report section completeness",
                value=report_section_completeness_rate,
                unit="ratio",
                target=">= 0.95",
                status=_status_min(report_section_completeness_rate, 0.95),
                score=_score_min(report_section_completeness_rate, 0.95),
                source="database",
                description=(
                    "Reports with non-empty root cause, impact, body markdown, "
                    "and structurally present timeline/actions/follow-up sections."
                ),
            ),
            _metric(
                key="report_version_issue_count",
                category="quality",
                label="Report version issue count",
                value=report_version_issue_count,
                unit="count",
                target="0",
                status=_status_zero(report_version_issue_count),
                score=_score_zero(report_version_issue_count),
                source="database",
                description=(
                    "Incidents whose report versions are not a contiguous sequence "
                    "starting at 1."
                ),
            ),
        ]

    def _tool_and_performance_metrics(
        self,
        *,
        runs: list[AgentRun],
        tool_calls: list[Any],
        nodes: list[Any],
        tool_total: int,
    ) -> list[EngineeringMetric]:
        tool_success_rate = _rate(
            sum(1 for call in tool_calls if call.status == "succeeded"), tool_total
        )
        tool_degraded_rate = _rate(
            sum(1 for call in tool_calls if call.status == "degraded"), tool_total
        )
        tool_cache_hit_rate = _rate(
            sum(1 for call in tool_calls if call.cache_hit), tool_total
        )
        tool_call_run_ids = {call.agent_run_id for call in tool_calls}
        tool_call_coverage_rate = _rate(
            sum(1 for run in runs if run.agent_run_id in tool_call_run_ids),
            len(runs),
        )
        diagnosis_duration_p95_ms = _p95(run.duration_ms for run in runs)
        tool_call_duration_p95_ms = _p95(call.duration_ms for call in tool_calls)
        node_duration_p95_ms = _p95(node.duration_ms for node in nodes)

        return [
            _metric(
                key="runtime_tool_success_rate",
                category="reliability",
                label="Runtime tool success rate",
                value=tool_success_rate,
                unit="ratio",
                target=">= 0.90",
                status=_status_min(tool_success_rate, 0.90),
                score=_score_min(tool_success_rate, 0.90),
                source="database",
                description=(
                    "Succeeded tool calls divided by all recorded tool calls in "
                    "the window."
                ),
            ),
            _metric(
                key="runtime_tool_degraded_rate",
                category="reliability",
                label="Runtime tool degraded rate",
                value=tool_degraded_rate,
                unit="ratio",
                target="<= 0.10",
                status=_status_max(tool_degraded_rate, 0.10),
                score=_score_max(tool_degraded_rate, 0.10),
                source="database",
                description="Degraded tool calls divided by all recorded tool calls in the window.",
            ),
            _metric(
                key="runtime_tool_cache_hit_rate",
                category="efficiency",
                label="Runtime tool cache hit rate",
                value=tool_cache_hit_rate,
                unit="ratio",
                target="track; warn below 0.50",
                status=_status_min(tool_cache_hit_rate, 0.50, fail="warn"),
                score=_score_min(tool_cache_hit_rate, 0.50),
                source="database",
                description="Tool calls served from cache divided by all recorded tool calls.",
            ),
            _metric(
                key="tool_call_coverage_rate",
                category="reliability",
                label="Tool call coverage",
                value=tool_call_coverage_rate,
                unit="ratio",
                target=">= 0.95",
                status=_status_min(tool_call_coverage_rate, 0.95),
                score=_score_min(tool_call_coverage_rate, 0.95),
                source="database",
                description="Agent runs with at least one persisted tool call trace.",
            ),
            _metric(
                key="diagnosis_duration_p95_ms",
                category="performance",
                label="Diagnosis duration P95",
                value=diagnosis_duration_p95_ms,
                unit="ms",
                target="<= 60000",
                status=_status_max(diagnosis_duration_p95_ms, 60000),
                score=_score_max(diagnosis_duration_p95_ms, 60000),
                source="database",
                description="P95 agent run duration for runs with recorded duration_ms.",
            ),
            _metric(
                key="tool_call_duration_p95_ms",
                category="performance",
                label="Tool call duration P95",
                value=tool_call_duration_p95_ms,
                unit="ms",
                target="<= 5000",
                status=_status_max(tool_call_duration_p95_ms, 5000),
                score=_score_max(tool_call_duration_p95_ms, 5000),
                source="database",
                description="P95 recorded tool call duration.",
            ),
            _metric(
                key="agent_node_duration_p95_ms",
                category="performance",
                label="Agent node duration P95",
                value=node_duration_p95_ms,
                unit="ms",
                target="<= 10000",
                status=_status_max(node_duration_p95_ms, 10000),
                score=_score_max(node_duration_p95_ms, 10000),
                source="database",
                description="P95 LangGraph node trace duration.",
            ),
        ]

    def _external_metrics(self) -> list[EngineeringMetric]:
        return [
            _unknown_metric(
                key="backend_test_coverage",
                category="maintainability",
                label="Backend test coverage",
                target="> 80% overall; >= 85% for core packages",
                source="ci_coverage",
                description=(
                    "Collected from pytest-cov XML/CI output, not from the "
                    "application database."
                ),
            ),
            _unknown_metric(
                key="guardrail_test_coverage",
                category="safety",
                label="Guardrail test coverage",
                target=">= 95%",
                source="ci_coverage",
                description=(
                    "Collected from pytest-cov package/file coverage for "
                    "packages/agent/guardrails."
                ),
            ),
            _unknown_metric(
                key="frontend_test_coverage",
                category="maintainability",
                label="Frontend test coverage",
                target="> 80% statements/branches/functions/lines",
                source="frontend_ci",
                description="Collected from Vitest coverage output.",
            ),
            _unknown_metric(
                key="ci_pipeline_status",
                category="delivery",
                label="CI pipeline status",
                target="passing",
                source="ci",
                description=(
                    "Collected from the CI provider; this API does not call "
                    "external CI systems."
                ),
            ),
            _unknown_metric(
                key="ruff_lint_status",
                category="maintainability",
                label="Ruff lint status",
                target="passing",
                source="static_analysis",
                description="Collected from local or CI ruff output.",
            ),
            _unknown_metric(
                key="mypy_type_check_status",
                category="maintainability",
                label="Mypy type check status",
                target="passing",
                source="static_analysis",
                description="Collected from local or CI mypy output.",
            ),
            _unknown_metric(
                key="dependency_vulnerability_status",
                category="safety",
                label="Dependency vulnerability status",
                target="no high or critical known vulnerabilities",
                source="security_scan",
                description="Collected from Python and frontend dependency scanners.",
            ),
            _unknown_metric(
                key="api_contract_test_status",
                category="quality",
                label="API contract test status",
                target="passing",
                source="contract_tests",
                description="Collected from contract tests for documented API behavior.",
            ),
            _unknown_metric(
                key="api_latency_p95_ms",
                category="performance",
                label="API latency P95",
                target="<= 300 ms for POST /api/alerts; <= 200 ms for read APIs",
                source="prometheus",
                description=(
                    "Collected from HTTP server metrics in Prometheus; no "
                    "request-duration table exists in the app DB."
                ),
            ),
            _unknown_metric(
                key="dora_deployment_frequency",
                category="delivery",
                label="DORA deployment frequency",
                target="team-defined",
                source="vcs_ci_cd",
                description=(
                    "Collected from deployment history outside the local incident "
                    "database."
                ),
            ),
            _unknown_metric(
                key="dora_lead_time_for_changes",
                category="delivery",
                label="DORA lead time for changes",
                target="team-defined",
                source="vcs_ci_cd",
                description="Collected from PR, commit, and deployment timestamps.",
            ),
            _unknown_metric(
                key="dora_change_failure_rate",
                category="delivery",
                label="DORA change failure rate",
                target="team-defined",
                source="vcs_ci_cd",
                description="Collected by correlating deployments with incidents and rollbacks.",
            ),
            _unknown_metric(
                key="dora_mttr",
                category="delivery",
                label="DORA MTTR",
                target="team-defined",
                source="incident_process",
                description="Collected from production incident lifecycle timestamps.",
            ),
        ]


def _metric(
    *,
    key: str,
    category: str,
    label: str,
    value: Any | None,
    unit: str | None,
    target: str | None,
    status: MetricStatus,
    score: float | None = None,
    weight: float = 1.0,
    source: str,
    description: str,
    reproduction: list[str] | None = None,
) -> EngineeringMetric:
    return EngineeringMetric(
        key=key,
        category=category,
        label=label,
        value=value,
        unit=unit,
        target=target,
        status=status,
        score=_default_score(status) if score is None else score,
        weight=weight,
        source=source,
        description=description,
        reproduction=reproduction or _SOURCE_REPRODUCTION.get(source, []),
    )


def _unknown_metric(
    *,
    key: str,
    category: str,
    label: str,
    target: str,
    source: str,
    description: str,
) -> EngineeringMetric:
    return _metric(
        key=key,
        category=category,
        label=label,
        value=None,
        unit=None,
        target=target,
        status="unknown",
        score=None,
        source=source,
        description=description,
    )


def _build_scorecard(metrics: list[EngineeringMetric]) -> EngineeringScorecard:
    metric_count = len(metrics)
    scored_metrics = [metric for metric in metrics if metric.score is not None]
    unknown_metric_count = sum(1 for metric in metrics if metric.status == "unknown")
    pass_count = sum(1 for metric in metrics if metric.status == "pass")
    warn_count = sum(1 for metric in metrics if metric.status == "warn")
    fail_count = sum(1 for metric in metrics if metric.status == "fail")
    completeness_rate = _round_rate(len(scored_metrics), metric_count)

    category_scores = [
        _build_category_score(category, weight, metrics)
        for category, weight in _CATEGORY_WEIGHTS.items()
    ]
    scored_categories = [
        category for category in category_scores if category.score is not None
    ]
    weight_total = sum(category.weight for category in scored_categories)
    if weight_total > 0:
        overall_score = round(
            sum((category.score or 0.0) * category.weight for category in scored_categories)
            / weight_total,
            1,
        )
    else:
        overall_score = None

    gate_status = _scorecard_status(
        metrics=metrics,
        overall_score=overall_score,
        completeness_rate=completeness_rate,
    )
    return EngineeringScorecard(
        overall_score=overall_score,
        gate_status=gate_status,
        completeness_rate=completeness_rate,
        metric_count=metric_count,
        scored_metric_count=len(scored_metrics),
        unknown_metric_count=unknown_metric_count,
        pass_count=pass_count,
        warn_count=warn_count,
        fail_count=fail_count,
        score_model=(
            "Scores are 0-100. Minimum-threshold metrics scale as value/target; "
            "maximum-threshold metrics scale as target/value; zero-violation "
            "safety metrics score 100 only at zero. Unknown external metrics are "
            "excluded from the weighted score and counted in completeness_rate."
        ),
        category_scores=category_scores,
        top_risks=_top_risks(metrics),
        reproduction=[
            "docker compose up -d postgres redis prometheus loki api",
            "curl http://localhost:8000/healthz",
            "curl http://localhost:8000/readyz",
            "curl http://localhost:8000/api/evals/engineering-metrics?window_days=30",
            "python -m packages.evals.runner --suite smoke --output reports/eval-smoke.json",
            (
                "pytest tests/unit tests/integration --cov=apps --cov=packages "
                "--cov-report=term-missing --cov-report=xml --cov-fail-under=80"
            ),
            "cd apps/web && npm run test:coverage && npm run test:e2e",
        ],
    )


def _build_category_score(
    category: str,
    weight: float,
    metrics: list[EngineeringMetric],
) -> EngineeringCategoryScore:
    category_metrics = [metric for metric in metrics if metric.category == category]
    scored = [metric for metric in category_metrics if metric.score is not None]
    if scored:
        score = round(
            sum((metric.score or 0.0) * metric.weight for metric in scored)
            / sum(metric.weight for metric in scored),
            1,
        )
    else:
        score = None
    fail_count = sum(1 for metric in category_metrics if metric.status == "fail")
    warn_count = sum(1 for metric in category_metrics if metric.status == "warn")
    unknown_count = sum(1 for metric in category_metrics if metric.status == "unknown")
    return EngineeringCategoryScore(
        category=category,
        weight=weight,
        score=score,
        status=_category_status(score, fail_count, warn_count, unknown_count),
        metric_count=len(category_metrics),
        scored_metric_count=len(scored),
        unknown_metric_count=unknown_count,
        fail_count=fail_count,
        warn_count=warn_count,
    )


def _category_status(
    score: float | None,
    fail_count: int,
    warn_count: int,
    unknown_count: int,
) -> MetricStatus:
    if score is None:
        return "unknown"
    if fail_count > 0 or score < 80:
        return "fail"
    if warn_count > 0 or unknown_count > 0 or score < 95:
        return "warn"
    return "pass"


def _scorecard_status(
    *,
    metrics: list[EngineeringMetric],
    overall_score: float | None,
    completeness_rate: float,
) -> MetricStatus:
    if overall_score is None:
        return "unknown"
    hard_fail = any(
        metric.key in _HARD_GATE_KEYS and metric.status == "fail"
        for metric in metrics
    )
    if hard_fail or overall_score < 80:
        return "fail"
    if (
        any(metric.status == "fail" for metric in metrics)
        or any(metric.status == "warn" for metric in metrics)
        or completeness_rate < 1.0
        or overall_score < 95
    ):
        return "warn"
    return "pass"


def _top_risks(metrics: list[EngineeringMetric]) -> list[str]:
    ranked = sorted(
        [
            metric for metric in metrics
            if metric.status in {"fail", "warn"}
        ],
        key=lambda metric: (
            0 if metric.status == "fail" else 1,
            101.0 if metric.score is None else metric.score,
            metric.key,
        ),
    )
    return [
        (
            f"{metric.label}: status={metric.status}, score={metric.score}, "
            f"value={metric.value}, target={metric.target}"
        )
        for metric in ranked[:5]
    ]


def _rate(numerator: int, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return round(numerator / denominator, 4)


def _round_rate(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return round(numerator / denominator, 4)


def _p95(values: Iterable[int | None]) -> int | None:
    cleaned = sorted(int(value) for value in values if value is not None)
    if not cleaned:
        return None
    index = max(0, ceil(len(cleaned) * 0.95) - 1)
    return cleaned[index]


def _status_min(
    value: Any | None,
    target: float,
    *,
    missing: MetricStatus = "unknown",
    fail: MetricStatus = "fail",
) -> MetricStatus:
    number = _as_float(value)
    if number is None:
        return missing
    return "pass" if number >= target else fail


def _status_max(
    value: Any | None,
    target: float,
    *,
    missing: MetricStatus = "unknown",
    fail: MetricStatus = "fail",
) -> MetricStatus:
    number = _as_float(value)
    if number is None:
        return missing
    return "pass" if number <= target else fail


def _status_zero(value: int, *, nonzero: MetricStatus = "fail") -> MetricStatus:
    return "pass" if value == 0 else nonzero


def _score_min(value: Any | None, target: float) -> float | None:
    number = _as_float(value)
    if number is None:
        return None
    if target <= 0:
        return None
    return round(max(0.0, min(100.0, number / target * 100.0)), 1)


def _score_max(value: Any | None, target: float) -> float | None:
    number = _as_float(value)
    if number is None:
        return None
    if number <= target:
        return 100.0
    if number <= 0:
        return 0.0
    return round(max(0.0, min(100.0, target / number * 100.0)), 1)


def _score_zero(value: int, *, nonzero_score: float = 0.0) -> float:
    return 100.0 if value == 0 else nonzero_score


def _default_score(status: MetricStatus) -> float | None:
    if status == "pass":
        return 100.0
    if status == "warn":
        return 70.0
    if status == "fail":
        return 0.0
    return None


def _as_float(value: Any | None) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _approval_action_risk(
    approval: Approval, action_by_id: dict[str, Action]
) -> str | None:
    action = action_by_id.get(approval.action_id)
    return action.risk_level if action is not None else None


def _l3_confirmation_valid(approval: Approval, action: Action) -> bool:
    return (
        approval.risk_ack is True
        and approval.confirm_action_type == action.type
        and approval.confirm_target == (action.target or "")
    )


def _run_has_traceable_evidence(
    run: AgentRun, evidence_by_run: set[str]
) -> bool:
    state = run.state if isinstance(run.state, dict) else {}
    root_cause = state.get("root_cause")
    if isinstance(root_cause, dict) and root_cause.get("evidence_ids"):
        return True
    return run.agent_run_id in evidence_by_run


def _evidence_ids_by_run(evidence_items: Iterable[Any]) -> dict[str, set[str]]:
    by_run: dict[str, set[str]] = {}
    for item in evidence_items:
        by_run.setdefault(item.agent_run_id, set()).add(item.evidence_id)
    return by_run


def _run_has_complete_evidence_records(
    run: AgentRun,
    evidence_ids_by_run: dict[str, set[str]],
) -> bool:
    persisted_ids = evidence_ids_by_run.get(run.agent_run_id, set())
    if not persisted_ids:
        return False
    state = run.state if isinstance(run.state, dict) else {}
    root_cause = state.get("root_cause")
    if not isinstance(root_cause, dict):
        return True
    evidence_ids = root_cause.get("evidence_ids")
    if not isinstance(evidence_ids, list):
        return True
    explicit_evidence_refs = {
        evidence_id
        for evidence_id in evidence_ids
        if isinstance(evidence_id, str) and evidence_id.startswith("evd_")
    }
    return explicit_evidence_refs.issubset(persisted_ids)


def _run_has_checkpoint_pointer(run: AgentRun) -> bool:
    return bool(run.checkpoint_thread_id and run.latest_checkpoint_id)


def _report_has_core_sections(report: IncidentReport) -> bool:
    return (
        bool(report.root_cause.strip())
        and bool(report.impact.strip())
        and bool(report.body_markdown.strip())
        and isinstance(report.timeline, list)
        and isinstance(report.actions, list)
        and isinstance(report.follow_ups, list)
    )


def _report_version_issue_count(reports: list[IncidentReport]) -> int:
    versions_by_incident: dict[str, list[int]] = {}
    for report in reports:
        versions_by_incident.setdefault(report.incident_id, []).append(report.version)
    issue_count = 0
    for versions in versions_by_incident.values():
        ordered = sorted(versions)
        expected = list(range(1, len(ordered) + 1))
        if ordered != expected:
            issue_count += 1
    return issue_count
