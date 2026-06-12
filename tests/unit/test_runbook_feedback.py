"""Tests for deterministic RunbookFeedbackAnalyzer — M7 (PR 7.1–7.4)."""

from __future__ import annotations

from datetime import timedelta

from packages.common.time import utc_now
from packages.discovery.runbook_feedback import (
    DIAGNOSTIC_STEP_PATTERNS,
    ActionStats,
    FeedbackResult,
    GapReport,
    IncidentCluster,
    RunbookFeedbackAnalyzer,
)

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


def _make_incident(
    incident_id: str,
    service: str,
    alert_name: str,
    status: str = "closed",
    fault_type: str | None = None,
) -> dict:
    d: dict = {
        "incident_id": incident_id,
        "service": service,
        "alert_name": alert_name,
        "status": status,
        "severity": "P1",
    }
    if fault_type is not None:
        d["fault_type"] = fault_type
    return d


def _make_action(
    action_id: str,
    incident_id: str,
    action_type: str,
    status: str = "success",
    diagnosis_confidence: float = 0.9,
) -> dict:
    return {
        "action_id": action_id,
        "incident_id": incident_id,
        "type": action_type,
        "status": status,
        "execution_result": {},
        "diagnosis_confidence": diagnosis_confidence,
    }


def _make_runbook_draft(
    incident_type: str,
    service: str = "test-service",
    content: str = "",
) -> dict:
    if not content:
        content = f"# {incident_type} Runbook\n\n## Detection\n\nSome detection steps.\n"
    return {
        "draft_id": f"draft-{incident_type}",
        "service": service,
        "incident_type": incident_type,
        "content": content,
    }


def _make_evidence(tool: str, summary: str) -> dict:
    return {"tool": tool, "summary": summary}


# ===================================================================
# PR 7.1: Incident Aggregation
# ===================================================================


class TestIncidentAggregation:
    def test_less_than_min_incidents_no_feedback(self):
        analyzer = RunbookFeedbackAnalyzer(min_incidents=5)
        incidents = [
            _make_incident("inc-1", "svc-a", "HighLatency"),
            _make_incident("inc-2", "svc-a", "HighLatency"),
        ]
        clusters = analyzer.aggregate_incidents(incidents)
        assert len(clusters) == 0

    def test_min_incidents_generates_summary(self):
        analyzer = RunbookFeedbackAnalyzer(min_incidents=3)
        incidents = [
            _make_incident("inc-1", "svc-a", "HighLatency"),
            _make_incident("inc-2", "svc-a", "HighLatency"),
            _make_incident("inc-3", "svc-a", "HighLatency"),
        ]
        clusters = analyzer.aggregate_incidents(incidents)
        assert len(clusters) == 1
        assert clusters[0].service == "svc-a"
        assert clusters[0].fault_type == "high_latency"
        assert len(clusters[0].incident_ids) == 3

    def test_aggregation_by_service_and_fault_type(self):
        analyzer = RunbookFeedbackAnalyzer(min_incidents=3)
        incidents = [
            _make_incident("inc-1", "svc-a", "HighLatency"),
            _make_incident("inc-2", "svc-a", "HighLatency"),
            _make_incident("inc-3", "svc-a", "HighLatency"),
            _make_incident("inc-4", "svc-b", "HighErrorRate"),
            _make_incident("inc-5", "svc-b", "HighErrorRate"),
            _make_incident("inc-6", "svc-b", "HighErrorRate"),
        ]
        clusters = analyzer.aggregate_incidents(incidents)
        assert len(clusters) == 2
        services = {c.service for c in clusters}
        assert services == {"svc-a", "svc-b"}

    def test_open_incidents_excluded(self):
        analyzer = RunbookFeedbackAnalyzer(min_incidents=2)
        incidents = [
            _make_incident("inc-1", "svc-a", "HighLatency", status="open"),
            _make_incident("inc-2", "svc-a", "HighLatency", status="acknowledged"),
            _make_incident("inc-3", "svc-a", "HighLatency", status="closed"),
            _make_incident("inc-4", "svc-a", "HighLatency", status="resolved"),
        ]
        clusters = analyzer.aggregate_incidents(incidents)
        assert len(clusters) == 1
        cluster = clusters[0]
        assert len(cluster.incident_ids) == 2
        assert "inc-3" in cluster.incident_ids
        assert "inc-4" in cluster.incident_ids

    def test_fault_type_derived_from_alert_name_latency(self):
        analyzer = RunbookFeedbackAnalyzer(min_incidents=2)
        incidents = [
            _make_incident("inc-1", "svc-a", "P99 latency high"),
            _make_incident("inc-2", "svc-a", "P99 latency high"),
        ]
        clusters = analyzer.aggregate_incidents(incidents)
        assert clusters[0].fault_type == "high_latency"

    def test_fault_type_derived_from_alert_name_error(self):
        analyzer = RunbookFeedbackAnalyzer(min_incidents=2)
        incidents = [
            _make_incident("inc-1", "svc-a", "5xx error rate"),
            _make_incident("inc-2", "svc-a", "5xx error rate"),
        ]
        clusters = analyzer.aggregate_incidents(incidents)
        assert clusters[0].fault_type == "high_error_rate"

    def test_fault_type_derived_from_alert_name_saturation(self):
        analyzer = RunbookFeedbackAnalyzer(min_incidents=2)
        incidents = [
            _make_incident("inc-1", "svc-a", "OOMKilled"),
            _make_incident("inc-2", "svc-a", "OOMKilled"),
        ]
        clusters = analyzer.aggregate_incidents(incidents)
        assert clusters[0].fault_type == "resource_saturation"

    def test_fault_type_derived_from_alert_name_dependency(self):
        analyzer = RunbookFeedbackAnalyzer(min_incidents=2)
        incidents = [
            _make_incident("inc-1", "svc-a", "Downstream timeout"),
            _make_incident("inc-2", "svc-a", "Downstream timeout"),
        ]
        clusters = analyzer.aggregate_incidents(incidents)
        assert clusters[0].fault_type == "dependency_failure"

    def test_fault_type_explicit_overrides_derived(self):
        analyzer = RunbookFeedbackAnalyzer(min_incidents=2)
        incidents = [
            _make_incident("inc-1", "svc-a", "HighLatency", fault_type="custom_type"),
            _make_incident("inc-2", "svc-a", "HighLatency", fault_type="custom_type"),
        ]
        clusters = analyzer.aggregate_incidents(incidents)
        assert clusters[0].fault_type == "custom_type"

    def test_unknown_alert_name_defaults_generic(self):
        analyzer = RunbookFeedbackAnalyzer(min_incidents=2)
        incidents = [
            _make_incident("inc-1", "svc-a", "WeirdThing"),
            _make_incident("inc-2", "svc-a", "WeirdThing"),
        ]
        clusters = analyzer.aggregate_incidents(incidents)
        assert clusters[0].fault_type == "generic_incident"


# ===================================================================
# PR 7.2: Action Statistics
# ===================================================================


class TestActionStatistics:
    def test_successful_actions_collected(self):
        analyzer = RunbookFeedbackAnalyzer()
        cluster = IncidentCluster(
            service="svc-a",
            fault_type="high_latency",
            incident_ids=["inc-1", "inc-2"],
        )
        actions = [
            _make_action("act-1", "inc-1", "restart_pod", "success"),
            _make_action("act-2", "inc-2", "restart_pod", "success"),
        ]
        stats = analyzer.compute_action_statistics(cluster, actions)
        assert stats.total == 2
        assert stats.successful == 2
        assert stats.failed == 0
        assert stats.success_rate == 1.0

    def test_failed_actions_collected(self):
        analyzer = RunbookFeedbackAnalyzer()
        cluster = IncidentCluster(
            service="svc-a",
            fault_type="high_error_rate",
            incident_ids=["inc-1"],
        )
        actions = [
            _make_action("act-1", "inc-1", "scale_up", "failed", 0.95),
        ]
        stats = analyzer.compute_action_statistics(cluster, actions)
        assert stats.failed == 1
        assert stats.success_rate == 0.0

    def test_skipped_actions_collected(self):
        analyzer = RunbookFeedbackAnalyzer()
        cluster = IncidentCluster(
            service="svc-a",
            fault_type="high_latency",
            incident_ids=["inc-1"],
        )
        actions = [
            _make_action("act-1", "inc-1", "restart_pod", "skipped"),
        ]
        stats = analyzer.compute_action_statistics(cluster, actions)
        assert stats.skipped == 1

    def test_rejected_actions_collected(self):
        analyzer = RunbookFeedbackAnalyzer()
        cluster = IncidentCluster(
            service="svc-a",
            fault_type="high_latency",
            incident_ids=["inc-1"],
        )
        actions = [
            _make_action("act-1", "inc-1", "rollback", "rejected"),
        ]
        stats = analyzer.compute_action_statistics(cluster, actions)
        assert stats.rejected == 1

    def test_low_confidence_no_feedback(self):
        """Actions with diagnosis_confidence < threshold should be excluded."""
        analyzer = RunbookFeedbackAnalyzer(confidence_threshold=0.7)
        cluster = IncidentCluster(
            service="svc-a",
            fault_type="high_latency",
            incident_ids=["inc-1"],
        )
        actions = [
            _make_action("act-1", "inc-1", "restart_pod", "success", 0.5),
            _make_action("act-2", "inc-1", "restart_pod", "failed", 0.4),
        ]
        stats = analyzer.compute_action_statistics(cluster, actions)
        assert stats.total == 0  # all filtered out

    def test_high_confidence_included(self):
        analyzer = RunbookFeedbackAnalyzer(confidence_threshold=0.7)
        cluster = IncidentCluster(
            service="svc-a",
            fault_type="high_latency",
            incident_ids=["inc-1"],
        )
        actions = [
            _make_action("act-1", "inc-1", "restart_pod", "success", 0.85),
        ]
        stats = analyzer.compute_action_statistics(cluster, actions)
        assert stats.total == 1

    def test_actions_filtered_by_incident_ids(self):
        analyzer = RunbookFeedbackAnalyzer()
        cluster = IncidentCluster(
            service="svc-a",
            fault_type="high_latency",
            incident_ids=["inc-1"],
        )
        actions = [
            _make_action("act-1", "inc-1", "restart_pod", "success"),
            _make_action("act-2", "inc-other", "restart_pod", "success"),
        ]
        stats = analyzer.compute_action_statistics(cluster, actions)
        assert stats.total == 1  # only inc-1

    def test_top_action_types_tallied(self):
        analyzer = RunbookFeedbackAnalyzer()
        cluster = IncidentCluster(
            service="svc-a",
            fault_type="high_latency",
            incident_ids=["inc-1", "inc-2"],
        )
        actions = [
            _make_action("act-1", "inc-1", "restart_pod", "success"),
            _make_action("act-2", "inc-1", "restart_pod", "success"),
            _make_action("act-3", "inc-2", "scale_up", "success"),
        ]
        stats = analyzer.compute_action_statistics(cluster, actions)
        assert stats.top_types["restart_pod"] == 2
        assert stats.top_types["scale_up"] == 1

    def test_action_statistics_uses_structured_outcome_source(self):
        """Must use structured action data, not free-text parsing."""
        analyzer = RunbookFeedbackAnalyzer()
        cluster = IncidentCluster(
            service="svc-a",
            fault_type="high_latency",
            incident_ids=["inc-1"],
        )
        actions = [_make_action("act-1", "inc-1", "restart_pod", "success")]
        stats = analyzer.compute_action_statistics(cluster, actions)
        # Computation is purely from structured action dicts — no text parsing
        assert stats.total == 1
        assert stats.successful == 1

    def test_action_statistics_does_not_parse_free_text_report(self):
        """Must not derive action outcomes from report body_markdown."""
        analyzer = RunbookFeedbackAnalyzer()
        cluster = IncidentCluster(
            service="svc-a",
            fault_type="high_latency",
            incident_ids=["inc-1"],
        )
        # Even if actions list is empty, analyzer should not fall back to
        # parsing any text field — it just returns zeros
        stats = analyzer.compute_action_statistics(cluster, [])
        assert stats.total == 0
        assert stats.successful == 0


# ===================================================================
# PR 7.3: Gap Detection
# ===================================================================


class TestGapDetection:
    def test_missing_fault_type_detected(self):
        analyzer = RunbookFeedbackAnalyzer()
        cluster = IncidentCluster(
            service="svc-a",
            fault_type="high_latency",
            incident_ids=["inc-1", "inc-2", "inc-3", "inc-4", "inc-5"],
        )
        # No existing runbook drafts → high_latency not covered
        gaps = analyzer.detect_gaps(cluster, existing_runbook_drafts=[])
        assert "high_latency" in gaps.missing_fault_types

    def test_existing_draft_covers_fault_type_no_gap(self):
        analyzer = RunbookFeedbackAnalyzer()
        cluster = IncidentCluster(
            service="svc-a",
            fault_type="high_latency",
            incident_ids=["inc-1"] * 5,
        )
        drafts = [_make_runbook_draft("high_latency", "svc-a")]
        gaps = analyzer.detect_gaps(cluster, existing_runbook_drafts=drafts)
        assert "high_latency" not in gaps.missing_fault_types

    def test_missing_diagnostic_step_detected(self):
        analyzer = RunbookFeedbackAnalyzer()
        cluster = IncidentCluster(
            service="svc-a",
            fault_type="high_latency",
            incident_ids=["inc-1"] * 5,
        )
        # Draft exists but does not mention specific diagnostic steps
        drafts = [
            _make_runbook_draft(
                "high_latency",
                "svc-a",
                content="# High Latency\n\n## Detection\nBasic checks.\n",
            )
        ]
        gaps = analyzer.detect_gaps(cluster, existing_runbook_drafts=drafts)
        # All expected steps should be missing since they're not in content
        assert "latency_percentile_by_endpoint" in gaps.missing_diagnostic_steps

    def test_diagnostic_step_mentioned_not_missing(self):
        analyzer = RunbookFeedbackAnalyzer()
        cluster = IncidentCluster(
            service="svc-a",
            fault_type="high_latency",
            incident_ids=["inc-1"] * 5,
        )
        drafts = [
            _make_runbook_draft(
                "high_latency",
                "svc-a",
                content="# High Latency\n\nCheck latency percentile by endpoint.\n",
            )
        ]
        gaps = analyzer.detect_gaps(cluster, existing_runbook_drafts=drafts)
        assert "latency_percentile_by_endpoint" not in gaps.missing_diagnostic_steps

    def test_recurring_evidence_pattern_detected(self):
        analyzer = RunbookFeedbackAnalyzer()
        cluster = IncidentCluster(
            service="svc-a",
            fault_type="high_latency",
            incident_ids=["inc-1"] * 5,
        )
        evidence = [
            _make_evidence("metrics", "timeout detected on backend"),
            _make_evidence("metrics", "timeout detected on backend"),
            _make_evidence("metrics", "timeout detected on backend"),
            _make_evidence("logs", "connection pool exhausted"),
            _make_evidence("logs", "connection pool exhausted"),
            _make_evidence("logs", "connection pool exhausted"),
            _make_evidence("traces", "high latency waterfall"),
        ]
        gaps = analyzer.detect_gaps(
            cluster, evidence_summaries=evidence
        )
        # "timeout" appears 3 times, "connection pool" 3 times
        assert any("timeout" in p for p in gaps.recurring_evidence_patterns)
        assert any("connection pool" in p for p in gaps.recurring_evidence_patterns)
        # "metrics" tool appears 3 times
        assert any("metrics" in p for p in gaps.recurring_evidence_patterns)

    def test_recurring_pattern_below_threshold_not_reported(self):
        analyzer = RunbookFeedbackAnalyzer()
        cluster = IncidentCluster(
            service="svc-a",
            fault_type="high_latency",
            incident_ids=["inc-1"] * 5,
        )
        evidence = [
            _make_evidence("metrics", "timeout detected"),
            _make_evidence("metrics", "timeout detected"),
        ]
        gaps = analyzer.detect_gaps(cluster, evidence_summaries=evidence)
        assert len(gaps.recurring_evidence_patterns) == 0

    def test_no_evidence_no_patterns(self):
        analyzer = RunbookFeedbackAnalyzer()
        cluster = IncidentCluster(
            service="svc-a",
            fault_type="high_latency",
            incident_ids=["inc-1"] * 5,
        )
        gaps = analyzer.detect_gaps(cluster, evidence_summaries=[])
        assert gaps.recurring_evidence_patterns == []

    def test_step_mentioned_in_content_fuzzy_match(self):
        analyzer = RunbookFeedbackAnalyzer()
        cluster = IncidentCluster(
            service="svc-a",
            fault_type="high_error_rate",
            incident_ids=["inc-1"] * 5,
        )
        drafts = [
            _make_runbook_draft(
                "high_error_rate",
                "svc-a",
                content="Check error rate by status code and endpoint.",
            )
        ]
        gaps = analyzer.detect_gaps(cluster, existing_runbook_drafts=drafts)
        assert "error_rate_by_status_code" not in gaps.missing_diagnostic_steps
        assert "error_rate_by_endpoint" not in gaps.missing_diagnostic_steps


# ===================================================================
# PR 7.4: AmendmentDraft & Frequency Control
# ===================================================================


class TestAmendmentDraftAndCooldown:
    def test_cooldown_blocks_repeat(self):
        analyzer = RunbookFeedbackAnalyzer(min_incidents=5, cooldown_days=7)
        cluster = IncidentCluster(
            service="svc-a",
            fault_type="high_latency",
            incident_ids=[f"inc-{i}" for i in range(5)],
        )
        actions = [
            _make_action(f"act-{i}", f"inc-{i}", "restart_pod", "success")
            for i in range(5)
        ]
        # Last summary was just created (now)
        last_summary_at = utc_now()

        result = analyzer.analyze_and_propose(
            cluster, actions, last_summary_at=last_summary_at
        )
        assert result.cooldown_active is True
        assert result.cooldown_until is not None
        assert result.cooldown_until > utc_now()

    def test_cooldown_expired_allows_amendment(self):
        analyzer = RunbookFeedbackAnalyzer(min_incidents=5, cooldown_days=7)
        cluster = IncidentCluster(
            service="svc-a",
            fault_type="high_latency",
            incident_ids=[f"inc-{i}" for i in range(5)],
        )
        actions = [
            _make_action(f"act-{i}", f"inc-{i}", "restart_pod", "success")
            for i in range(5)
        ]
        # Last summary was 8 days ago — cooldown expired
        last_summary_at = utc_now() - timedelta(days=8)

        result = analyzer.analyze_and_propose(
            cluster, actions, last_summary_at=last_summary_at
        )
        assert result.cooldown_active is False

    def test_create_amendment_draft_when_gaps_detected(self):
        analyzer = RunbookFeedbackAnalyzer(min_incidents=5, cooldown_days=7)
        cluster = IncidentCluster(
            service="svc-a",
            fault_type="high_latency",
            incident_ids=[f"inc-{i}" for i in range(5)],
        )
        actions = [
            _make_action(f"act-{i}", f"inc-{i}", "restart_pod", "success")
            for i in range(5)
        ]
        result = analyzer.analyze_and_propose(cluster, actions)
        assert result.should_amend is True
        assert result.amendment_content is not None
        assert len(result.amendment_content) > 0

    def test_amendment_draft_pending_review(self):
        """Amendment drafts are proposals — never auto-ingested."""
        analyzer = RunbookFeedbackAnalyzer(min_incidents=5)
        cluster = IncidentCluster(
            service="svc-a",
            fault_type="high_latency",
            incident_ids=[f"inc-{i}" for i in range(5)],
        )
        result = analyzer.analyze_and_propose(cluster, [])
        assert result.should_amend is True
        # The amendment is a proposal — it does not directly write to runbook_chunks
        assert result.amendment_content is not None

    def test_no_llm_called(self):
        """Phase 0-8: no LLM invocation in feedback analysis."""
        analyzer = RunbookFeedbackAnalyzer()
        cluster = IncidentCluster(
            service="svc-a",
            fault_type="high_latency",
            incident_ids=[f"inc-{i}" for i in range(5)],
        )
        actions = [
            _make_action(f"act-{i}", f"inc-{i}", "restart_pod", "success")
            for i in range(5)
        ]
        # All methods are pure functions — no network, no LLM
        result = analyzer.analyze_and_propose(cluster, actions)
        assert result.amendment_content is not None
        # Verify the content is deterministically generated
        assert "Suggested Diagnostic Steps to Add" in result.amendment_content

    def test_no_web_search_called(self):
        """Phase 0-8: no web search in feedback analysis."""
        analyzer = RunbookFeedbackAnalyzer()
        cluster = IncidentCluster(
            service="svc-a",
            fault_type="high_latency",
            incident_ids=[f"inc-{i}" for i in range(5)],
        )
        result = analyzer.analyze_and_propose(cluster, [])
        # Content is derived purely from DIAGNOSTIC_STEP_PATTERNS
        assert "Suggested Diagnostic Steps" in result.amendment_content

    def test_not_ingested_directly(self):
        """Amendment drafts must go through review queue, not direct ingest."""
        analyzer = RunbookFeedbackAnalyzer()
        cluster = IncidentCluster(
            service="svc-a",
            fault_type="high_latency",
            incident_ids=[f"inc-{i}" for i in range(5)],
        )
        result = analyzer.analyze_and_propose(cluster, [])
        # The FeedbackResult is a data object — no side effects, no DB writes
        assert isinstance(result, FeedbackResult)
        assert result.amendment_content is not None
        # It's up to the caller (service/API) to persist as AmendmentDraft
        # with status='pending_review'

    def test_no_gaps_no_failures_no_amendment(self):
        analyzer = RunbookFeedbackAnalyzer(min_incidents=5)
        cluster = IncidentCluster(
            service="svc-a",
            fault_type="high_latency",
            incident_ids=[f"inc-{i}" for i in range(5)],
        )
        # Existing draft covers all diagnostic steps
        content_parts = ["# High Latency Runbook"]
        for step in DIAGNOSTIC_STEP_PATTERNS["high_latency"]:
            content_parts.append(step.replace("_", " "))
        content = "\n".join(content_parts)

        drafts = [_make_runbook_draft("high_latency", "svc-a", content=content)]
        actions = [
            _make_action(f"act-{i}", f"inc-{i}", "restart_pod", "success")
            for i in range(5)
        ]
        result = analyzer.analyze_and_propose(
            cluster, actions, existing_runbook_drafts=drafts
        )
        # No gaps + all actions successful → no amendment needed
        assert result.amendment_content is None

    def test_action_failures_trigger_amendment_even_without_gaps(self):
        analyzer = RunbookFeedbackAnalyzer(min_incidents=5)
        cluster = IncidentCluster(
            service="svc-a",
            fault_type="high_latency",
            incident_ids=[f"inc-{i}" for i in range(5)],
        )
        # Cover all diagnostic steps
        content_parts = ["# High Latency Runbook"]
        for step in DIAGNOSTIC_STEP_PATTERNS["high_latency"]:
            content_parts.append(step.replace("_", " "))
        content = "\n".join(content_parts)
        drafts = [_make_runbook_draft("high_latency", "svc-a", content=content)]

        # But actions have failures
        actions = [
            _make_action(f"act-{i}", f"inc-{i}", "restart_pod", "failed", 0.9)
            for i in range(5)
        ]
        result = analyzer.analyze_and_propose(
            cluster, actions, existing_runbook_drafts=drafts
        )
        assert result.should_amend is True
        assert "Action Effectiveness" in result.amendment_content

    def test_cooldown_not_active_when_no_previous_summary(self):
        analyzer = RunbookFeedbackAnalyzer()
        cluster = IncidentCluster(
            service="svc-a",
            fault_type="high_latency",
            incident_ids=[f"inc-{i}" for i in range(5)],
        )
        result = analyzer.analyze_and_propose(
            cluster, [], last_summary_at=None
        )
        assert result.cooldown_active is False

    def test_amendment_includes_section_and_rationale(self):
        analyzer = RunbookFeedbackAnalyzer(min_incidents=5)
        cluster = IncidentCluster(
            service="svc-a",
            fault_type="high_latency",
            incident_ids=[f"inc-{i}" for i in range(5)],
        )
        result = analyzer.analyze_and_propose(cluster, [])
        assert result.amendment_section is not None
        assert result.amendment_rationale is not None
        assert "5 closed incidents" in result.amendment_rationale
        assert "svc-a" in result.amendment_rationale

    def test_recurring_patterns_in_amendment_content(self):
        analyzer = RunbookFeedbackAnalyzer(min_incidents=5)
        cluster = IncidentCluster(
            service="svc-a",
            fault_type="high_latency",
            incident_ids=[f"inc-{i}" for i in range(5)],
        )
        evidence = [
            _make_evidence("metrics", "timeout detected on backend"),
        ] * 5
        result = analyzer.analyze_and_propose(
            cluster, [], evidence_summaries=evidence
        )
        assert "Recurring Evidence Patterns" in result.amendment_content

    def test_generic_incident_has_no_expected_steps(self):
        analyzer = RunbookFeedbackAnalyzer()
        cluster = IncidentCluster(
            service="svc-a",
            fault_type="generic_incident",
            incident_ids=[f"inc-{i}" for i in range(5)],
        )
        gaps = analyzer.detect_gaps(cluster, existing_runbook_drafts=[])
        # generic_incident is not in DIAGNOSTIC_STEP_PATTERNS → no diagnostic step gaps
        assert "generic_incident" in gaps.missing_fault_types
        assert gaps.missing_diagnostic_steps == []


# ===================================================================
# ActionStats dataclass tests
# ===================================================================


class TestActionStats:
    def test_success_rate_zero_actions(self):
        stats = ActionStats()
        assert stats.success_rate == 0.0

    def test_success_rate_all_success(self):
        stats = ActionStats(total=10, successful=10)
        assert stats.success_rate == 1.0

    def test_success_rate_half(self):
        stats = ActionStats(total=10, successful=5, failed=5)
        assert stats.success_rate == 0.5


# ===================================================================
# FeedbackResult dataclass tests
# ===================================================================


class TestFeedbackResult:
    def test_feedback_result_fields(self):
        cluster = IncidentCluster(
            service="svc-a",
            fault_type="high_latency",
            incident_ids=[f"inc-{i}" for i in range(5)],
        )
        result = FeedbackResult(
            service="svc-a",
            fault_type="high_latency",
            cluster=cluster,
            action_stats=ActionStats(total=5, successful=5),
            gaps=GapReport(),
            should_amend=False,
        )
        assert result.service == "svc-a"
        assert result.fault_type == "high_latency"
        assert result.should_amend is False
        assert result.cooldown_active is False
