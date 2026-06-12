"""Deterministic Runbook Feedback Analyzer — PR 7.1–7.4.

Phase 0-8: no LLM, no web_search. Feedback is derived purely from structured
incident data, action outcomes, and existing runbook coverage.

Workflow:
  1. Incident Aggregation (PR 7.1): group closed incidents by (service, fault_type).
  2. Action Statistics (PR 7.2): tally action outcomes for confident diagnoses.
  3. Gap Detection (PR 7.3): detect missing fault types, diagnostic steps,
     recurring evidence patterns.
  4. AmendmentDraft + Frequency Control (PR 7.4): generate feedback summary
     and proposed amendment draft, gated by cooldown.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from packages.common.time import utc_now

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes for feedback analysis
# ---------------------------------------------------------------------------


@dataclass
class ActionStats:
    """Aggregated action outcome statistics for a (service, fault_type) group."""

    total: int = 0
    successful: int = 0
    failed: int = 0
    skipped: int = 0
    rejected: int = 0
    top_types: Counter[str] = field(default_factory=Counter)

    @property
    def success_rate(self) -> float:
        if self.total == 0:
            return 0.0
        return self.successful / self.total


@dataclass
class IncidentCluster:
    """A group of incidents sharing the same (service, fault_type)."""

    service: str
    fault_type: str
    incident_ids: list[str] = field(default_factory=list)
    action_stats: ActionStats = field(default_factory=ActionStats)


@dataclass
class GapReport:
    """Detected gaps when comparing incident history to existing runbook coverage."""

    missing_fault_types: list[str] = field(default_factory=list)
    missing_diagnostic_steps: list[str] = field(default_factory=list)
    recurring_evidence_patterns: list[str] = field(default_factory=list)


@dataclass
class FeedbackResult:
    """Complete feedback analysis output for one (service, fault_type) group."""

    service: str
    fault_type: str
    cluster: IncidentCluster
    action_stats: ActionStats
    gaps: GapReport
    should_amend: bool
    cooldown_active: bool = False
    cooldown_until: datetime | None = None
    amendment_section: str | None = None
    amendment_rationale: str | None = None
    amendment_content: str | None = None


# ---------------------------------------------------------------------------
# Known diagnostic step patterns — used for gap detection
# ---------------------------------------------------------------------------

DIAGNOSTIC_STEP_PATTERNS: dict[str, list[str]] = {
    "high_latency": [
        "latency_percentile_by_endpoint",
        "dependency_latency_waterfall",
        "cpu_throttle_correlation",
        "gc_pause_analysis",
        "connection_pool_utilization",
        "recent_deployment_correlation",
    ],
    "high_error_rate": [
        "error_rate_by_status_code",
        "error_rate_by_endpoint",
        "dependency_error_rate",
        "recent_config_change_correlation",
    ],
    "resource_saturation": [
        "cpu_utilization",
        "memory_utilization",
        "disk_utilization",
        "connection_pool_utilization",
        "goroutine_thread_count",
    ],
    "dependency_failure": [
        "dependency_error_rate",
        "dependency_latency",
        "circuit_breaker_state",
        "dependency_health_check",
    ],
}

_KNOWN_FAULT_TYPES = frozenset(DIAGNOSTIC_STEP_PATTERNS.keys())


# ---------------------------------------------------------------------------
# RunbookFeedbackAnalyzer
# ---------------------------------------------------------------------------


class RunbookFeedbackAnalyzer:
    """Deterministic feedback analyzer for runbooks.

    Consumes closed incident data and existing runbook coverage to produce:
      - Incident clusters (aggregation by service + fault_type)
      - Action statistics per cluster
      - Gap detection vs. existing runbooks
      - AmendmentDraft proposals with cooldown control

    All methods are pure functions of their inputs — no LLM, no web_search,
    no side effects.
    """

    def __init__(
        self,
        *,
        min_incidents: int = 5,
        cooldown_days: int = 7,
        confidence_threshold: float = 0.7,
    ) -> None:
        self.min_incidents = min_incidents
        self.cooldown_days = cooldown_days
        self.confidence_threshold = confidence_threshold

    # ------------------------------------------------------------------
    # PR 7.1: Incident Aggregation
    # ------------------------------------------------------------------

    def aggregate_incidents(
        self,
        incidents: list[dict[str, Any]],
    ) -> list[IncidentCluster]:
        """Group incidents by (service, fault_type).

        Args:
            incidents: List of incident dicts. Each must have at least:
                incident_id, service, fault_type, status.
                Only incidents with status in ('closed', 'resolved') are included.

        Returns:
            IncidentCluster list, one per (service, fault_type) group.
        """
        groups: dict[tuple[str, str], IncidentCluster] = {}

        for inc in incidents:
            status = inc.get("status", "")
            if status not in ("closed", "resolved"):
                continue

            service = inc.get("service", "unknown")
            fault_type = inc.get("fault_type") or self._derive_fault_type(inc)
            key = (service, fault_type)

            if key not in groups:
                groups[key] = IncidentCluster(service=service, fault_type=fault_type)

            groups[key].incident_ids.append(inc.get("incident_id", ""))

        return [c for c in groups.values() if len(c.incident_ids) >= self.min_incidents]

    @staticmethod
    def _derive_fault_type(incident: dict[str, Any]) -> str:
        """Derive fault_type from alert_name when not explicitly set.

        Maps common alert name patterns to canonical incident types.
        """
        alert_name = incident.get("alert_name", "")
        alert_lower = alert_name.lower()

        if any(kw in alert_lower for kw in ("latency", "slow", "p99", "p95")):
            return "high_latency"
        if any(kw in alert_lower for kw in ("error", "5xx", "500", "failure")):
            return "high_error_rate"
        if any(kw in alert_lower for kw in ("cpu", "memory", "disk", "saturation", "oom")):
            return "resource_saturation"
        if any(kw in alert_lower for kw in ("dependency", "downstream", "upstream")):
            return "dependency_failure"

        return "generic_incident"

    # ------------------------------------------------------------------
    # PR 7.2: Action Statistics
    # ------------------------------------------------------------------

    def compute_action_statistics(
        self,
        cluster: IncidentCluster,
        actions: list[dict[str, Any]],
        *,
        confident_only: bool = True,
    ) -> ActionStats:
        """Compute action outcome statistics for an incident cluster.

        Args:
            cluster: The incident cluster to compute stats for.
            actions: List of action dicts. Each must have at least:
                incident_id, type, status, execution_result.
            confident_only: If True, only count actions from incidents
                whose diagnosis confidence >= threshold.

        Returns:
            ActionStats with aggregated counts.
        """
        incident_ids = set(cluster.incident_ids)

        stats = ActionStats()
        for action in actions:
            if action.get("incident_id") not in incident_ids:
                continue

            if confident_only:
                confidence = action.get("diagnosis_confidence", 0.0)
                if confidence < self.confidence_threshold:
                    continue

            stats.total += 1
            action_status = action.get("status", "unknown")

            if action_status == "success":
                stats.successful += 1
            elif action_status in ("failed", "failure", "error"):
                stats.failed += 1
            elif action_status == "skipped":
                stats.skipped += 1
            elif action_status == "rejected":
                stats.rejected += 1

            action_type = action.get("type", "unknown")
            stats.top_types[action_type] += 1

        return stats

    # ------------------------------------------------------------------
    # PR 7.3: Gap Detection
    # ------------------------------------------------------------------

    def detect_gaps(
        self,
        cluster: IncidentCluster,
        *,
        existing_runbook_drafts: list[dict[str, Any]] | None = None,
        evidence_summaries: list[dict[str, Any]] | None = None,
    ) -> GapReport:
        """Detect gaps between incident history and existing runbook coverage.

        Args:
            cluster: The incident cluster to analyze.
            existing_runbook_drafts: Existing runbook drafts for this
                (service, fault_type) pair. Each dict should have at least
                'incident_type' and 'content'.
            evidence_summaries: Evidence items collected across the cluster
                incidents. Used to detect recurring evidence patterns.

        Returns:
            GapReport with detected gaps.
        """
        gaps = GapReport()

        existing_drafts = existing_runbook_drafts or []
        covered_types: set[str] = set()
        covered_content_hints: set[str] = set()

        for draft in existing_drafts:
            covered_types.add(draft.get("incident_type", ""))
            content = draft.get("content", "")
            for step_name in DIAGNOSTIC_STEP_PATTERNS.get(
                draft.get("incident_type", ""), []
            ):
                if self._step_mentioned_in_content(step_name, content):
                    covered_content_hints.add(step_name)

        # Missing fault types
        if cluster.fault_type not in _KNOWN_FAULT_TYPES:
            gaps.missing_fault_types.append(cluster.fault_type)
        elif cluster.fault_type not in covered_types:
            gaps.missing_fault_types.append(cluster.fault_type)

        # Missing diagnostic steps
        expected_steps = DIAGNOSTIC_STEP_PATTERNS.get(cluster.fault_type, [])
        for step in expected_steps:
            if step not in covered_content_hints:
                gaps.missing_diagnostic_steps.append(step)

        # Recurring evidence patterns
        if evidence_summaries:
            gaps.recurring_evidence_patterns = self._detect_recurring_patterns(
                evidence_summaries
            )

        return gaps

    @staticmethod
    def _step_mentioned_in_content(step_name: str, content: str) -> bool:
        """Check if a diagnostic step is mentioned in runbook content."""
        content_lower = content.lower()
        hints = step_name.lower().replace("_", " ").split()
        return all(hint in content_lower for hint in hints)

    @staticmethod
    def _detect_recurring_patterns(
        evidence_summaries: list[dict[str, Any]],
        min_occurrences: int = 3,
    ) -> list[str]:
        """Detect recurring evidence patterns across incidents.

        Looks for evidence types or keywords that appear across multiple
        incidents, indicating a pattern worth documenting.

        Args:
            evidence_summaries: List of evidence dicts.
            min_occurrences: Minimum times a pattern must appear to be flagged.

        Returns:
            List of pattern descriptions.
        """
        tool_counter: Counter[str] = Counter()
        keyword_counter: Counter[str] = Counter()

        for ev in evidence_summaries:
            tool = ev.get("tool", "")
            if tool:
                tool_counter[tool] += 1

            summary = ev.get("summary", "") or ev.get("output_summary", "")
            if summary:
                for kw in _RECURRING_KEYWORDS:
                    if kw in summary.lower():
                        keyword_counter[kw] += 1

        patterns: list[str] = []
        for tool, count in tool_counter.most_common():
            if count >= min_occurrences:
                patterns.append(f"recurring_evidence_tool:{tool} (seen in {count} incidents)")

        for kw, count in keyword_counter.most_common(5):
            if count >= min_occurrences:
                patterns.append(f"recurring_pattern:{kw} (seen in {count} incidents)")

        return patterns

    # ------------------------------------------------------------------
    # PR 7.4: AmendmentDraft & Frequency Control
    # ------------------------------------------------------------------

    def analyze_and_propose(
        self,
        cluster: IncidentCluster,
        actions: list[dict[str, Any]],
        *,
        existing_runbook_drafts: list[dict[str, Any]] | None = None,
        evidence_summaries: list[dict[str, Any]] | None = None,
        last_summary_at: datetime | None = None,
    ) -> FeedbackResult:
        """Full feedback analysis: aggregate → stats → gaps → amendment proposal.

        This is the main entry point for PR 7.4. It orchestrates all three
        analysis steps and produces an amendment proposal if warranted.

        Args:
            cluster: Incident cluster to analyze.
            actions: Action records across all incidents.
            existing_runbook_drafts: Currently approved runbook drafts.
            evidence_summaries: Evidence collected during diagnosis.
            last_summary_at: Timestamp of the last feedback summary for this
                (service, fault_type) pair, for cooldown enforcement.

        Returns:
            FeedbackResult with gap report and amendment proposal.
        """
        # PR 7.2: Action statistics
        action_stats = self.compute_action_statistics(cluster, actions)

        # PR 7.3: Gap detection
        gaps = self.detect_gaps(
            cluster,
            existing_runbook_drafts=existing_runbook_drafts,
            evidence_summaries=evidence_summaries,
        )

        # PR 7.4: Cooldown check
        now = utc_now()
        cooldown_active = False
        cooldown_until = None
        if last_summary_at is not None:
            cooldown_until = last_summary_at + timedelta(days=self.cooldown_days)
            if now < cooldown_until:
                cooldown_active = True

        # Determine if amendment is warranted
        has_gaps = bool(
            gaps.missing_fault_types
            or gaps.missing_diagnostic_steps
            or gaps.recurring_evidence_patterns
        )
        has_action_failures = action_stats.failed > 0 or action_stats.rejected > 0
        should_amend = has_gaps or has_action_failures

        # Build amendment proposal
        amendment_section = None
        amendment_rationale = None
        amendment_content = None

        if should_amend and not cooldown_active:
            section, rationale, content = self._build_amendment(
                cluster, action_stats, gaps
            )
            amendment_section = section
            amendment_rationale = rationale
            amendment_content = content

        return FeedbackResult(
            service=cluster.service,
            fault_type=cluster.fault_type,
            cluster=cluster,
            action_stats=action_stats,
            gaps=gaps,
            should_amend=should_amend,
            cooldown_active=cooldown_active,
            cooldown_until=cooldown_until,
            amendment_section=amendment_section,
            amendment_rationale=amendment_rationale,
            amendment_content=amendment_content,
        )

    def _build_amendment(
        self,
        cluster: IncidentCluster,
        action_stats: ActionStats,
        gaps: GapReport,
    ) -> tuple[str | None, str | None, str | None]:
        """Build a deterministic amendment proposal from analysis results.

        Returns:
            Tuple of (section, rationale, content) for the amendment.
        """
        parts: list[str] = []

        # Add missing diagnostic steps
        if gaps.missing_diagnostic_steps:
            parts.append("## Suggested Diagnostic Steps to Add\n")
            for step in gaps.missing_diagnostic_steps:
                human_name = step.replace("_", " ").title()
                parts.append(f"- **{human_name}**: [To be filled by reviewer]")
            parts.append("")

        # Add action outcome notes
        if action_stats.failed > 0:
            parts.append("## Action Effectiveness\n")
            parts.append(
                f"- {action_stats.failed}/{action_stats.total} actions failed "
                f"({action_stats.failed / max(action_stats.total, 1) * 100:.0f}% failure rate)\n"
            )
            parts.append(
                "- Review the action plan section for this incident type "
                "to improve action reliability.\n"
            )
            parts.append("")

        # Add recurring evidence patterns
        if gaps.recurring_evidence_patterns:
            parts.append("## Recurring Evidence Patterns\n")
            for pattern in gaps.recurring_evidence_patterns:
                parts.append(f"- {pattern}")
            parts.append("")

        if not parts:
            return None, None, None

        content = "\n".join(parts).strip() + "\n"

        rationale_parts = [
            f"Deterministic feedback analysis based on {len(cluster.incident_ids)} "
            f"closed incidents for service '{cluster.service}' with fault type "
            f"'{cluster.fault_type}'.",
        ]
        if action_stats.total > 0:
            rationale_parts.append(
                f"Action success rate: {action_stats.success_rate:.0%} "
                f"({action_stats.successful}/{action_stats.total})."
            )
        if gaps.missing_diagnostic_steps:
            rationale_parts.append(
                f"Missing diagnostic steps: {', '.join(gaps.missing_diagnostic_steps)}."
            )

        section = "Diagnosis & Action Plan"
        rationale = " ".join(rationale_parts)

        return section, rationale, content


# ---------------------------------------------------------------------------
# Evidence keywords for recurring pattern detection
# ---------------------------------------------------------------------------

_RECURRING_KEYWORDS: list[str] = [
    "timeout",
    "connection refused",
    "rate limit",
    "circuit breaker",
    "oom",
    "out of memory",
    "disk full",
    "cpu throttled",
    "tls error",
    "certificate expired",
    "permission denied",
    "quota exceeded",
    "deadline exceeded",
    "unavailable",
    "backend connection",
    "connection pool",
]
