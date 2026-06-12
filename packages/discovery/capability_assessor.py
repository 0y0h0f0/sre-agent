"""CapabilityAssessor — standardized degradation output for discovery results.

M3 PR 3.2: Assesses capability gaps, degraded signals, fallback usage, and
confidence adjustments from a DiscoveryResult. Produces structured degradation
metadata for downstream consumers.

Design principle: degradation metadata is computed deterministically from the
DiscoveryResult — no additional API calls or side effects.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from packages.discovery.models import (
    SEMANTIC_PATTERNS,
    DiscoveryResult,
    SemanticType,
)


@dataclass
class DegradationReport:
    """Standardized degradation assessment for a DiscoveryResult."""

    # Gaps: capabilities that are entirely missing.
    capability_gaps: list[str] = field(default_factory=list)

    # Signals that are degraded but still partially available.
    degraded_signals: list[str] = field(default_factory=list)

    # Signals that are using a fallback (e.g., default service label).
    used_fallback_signals: list[str] = field(default_factory=list)

    # Confidence adjustment factor (0.0–1.0) applied due to degradation.
    # 1.0 = no adjustment needed; lower = confidence reduced.
    confidence_adjustment: float = 1.0

    # Per-service breakdown of gaps and degraded signals.
    per_service: dict[str, dict[str, Any]] = field(default_factory=dict)

    # Overall assessment: healthy | degraded | critical
    overall: str = "healthy"


class CapabilityAssessor:
    """Assesses capability coverage and degradation from a DiscoveryResult.

    Usage::

        assessor = CapabilityAssessor()
        report = assessor.assess(discovery_result)
        # report.capability_gaps, report.degraded_signals, etc.
    """

    # Semantic types that are considered "core" — missing any is a critical gap.
    _CORE_SEMANTIC_TYPES: set[SemanticType] = {"latency", "error_rate", "qps"}

    # Semantic types that are "extended" — missing is degraded but not critical.
    _EXTENDED_SEMANTIC_TYPES: set[SemanticType] = {"cpu_throttle", "disk_avail"}

    def __init__(
        self,
        *,
        core_types: set[SemanticType] | None = None,
        extended_types: set[SemanticType] | None = None,
    ) -> None:
        self._core = core_types or self._CORE_SEMANTIC_TYPES
        self._extended = extended_types or self._EXTENDED_SEMANTIC_TYPES

    def assess(self, result: DiscoveryResult) -> DegradationReport:
        """Produce a standardized degradation report from a DiscoveryResult."""
        gaps: list[str] = []
        degraded: list[str] = list(result.degraded_signals)
        fallbacks: list[str] = []
        per_service: dict[str, dict[str, Any]] = {}

        # --- 1. Backend-level degradation (from runner) ---
        # Already in result.degraded_signals — copy over.

        # --- 2. Metric type gaps ---
        available_types: set[str] = {
            m.semantic_type
            for m in result.metric_mappings
            if m.status == "available"
        }
        degraded_types: set[str] = {
            m.semantic_type
            for m in result.metric_mappings
            if m.status == "degraded"
        }
        all_expected = set(SEMANTIC_PATTERNS.keys())

        for stype in all_expected:
            if stype in available_types:
                continue
            if stype in degraded_types:
                degraded.append(f"metric_{stype}_degraded")
            else:
                if stype in self._core:
                    gaps.append(f"metric_{stype}_missing")
                elif stype in self._extended:
                    degraded.append(f"metric_{stype}_missing")

        # --- 3. Service label fallback detection ---
        # Check if there are warnings about using default service labels.
        for warning in result.warnings:
            if "using default" in warning.lower():
                if "service label" in warning.lower():
                    if "prometheus" in warning.lower():
                        fallbacks.append("prometheus_service_label_fallback")
                    elif "loki" in warning.lower():
                        fallbacks.append("loki_service_label_fallback")

        # --- 4. Per-service capability assessment ---
        for cap in result.capability_matrix:
            svc_gaps: list[str] = list(cap.capability_gaps)
            svc_degraded: list[str] = []

            # Check metric coverage per service.
            if cap.metrics_available:
                svc_metric_types = {m.semantic_type for m in cap.metric_mappings}
                for stype in self._core:
                    if stype not in svc_metric_types:
                        svc_gaps.append(f"metric_{stype}_missing")
                for stype in self._extended:
                    if stype not in svc_metric_types:
                        svc_degraded.append(f"metric_{stype}_degraded")
            else:
                svc_gaps.append("metrics_unavailable")

            per_service[cap.service_name] = {
                "gaps": svc_gaps,
                "degraded": svc_degraded,
                "has_metrics": cap.metrics_available,
                "has_logs": cap.logs_available,
                "has_traces": cap.traces_available,
                "has_k8s": cap.k8s_accessible,
            }

        # --- 5. Confidence adjustment ---
        adjustment = self._compute_confidence_adjustment(
            num_gaps=len(gaps),
            num_degraded=len(degraded),
            num_fallbacks=len(fallbacks),
            total_services=len(per_service),
        )

        # --- 6. Overall assessment ---
        core_gaps = [g for g in gaps if any(ct in g for ct in self._core)]
        if core_gaps or result.status == "failed":
            overall = "critical"
        elif gaps or degraded or result.status == "degraded":
            overall = "degraded"
        else:
            overall = "healthy"

        return DegradationReport(
            capability_gaps=gaps,
            degraded_signals=degraded,
            used_fallback_signals=fallbacks,
            confidence_adjustment=adjustment,
            per_service=per_service,
            overall=overall,
        )

    @staticmethod
    def _compute_confidence_adjustment(
        *,
        num_gaps: int,
        num_degraded: int,
        num_fallbacks: int,
        total_services: int,
    ) -> float:
        """Compute a multiplicative confidence adjustment factor.

        Each gap, degraded signal, or fallback reduces confidence slightly.
        The adjustment is bounded at 0.3 (cannot go below 30% confidence).
        """
        penalty = num_gaps * 0.15 + num_degraded * 0.05 + num_fallbacks * 0.10
        if total_services > 0 and num_gaps > 0:
            # Extra penalty if gaps affect many services.
            penalty += min(0.10, num_gaps / total_services * 0.05)
        return max(0.30, 1.0 - penalty)
