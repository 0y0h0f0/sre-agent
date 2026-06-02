"""Evidence cross-validation (roadmap Phase 1.3).

Corroborate metrics / logs / traces / deployment signals to raise or lower
diagnostic confidence and flag contradictions for human review. Pure and
deterministic — no LLM, no network.

Design rules:
- **Weights**: Trace > Metrics > Logs > Git/deployment.
- **Corroboration** (>= 2 independent anomaly signals) raises confidence.
- **Conflict** (anomaly and healthy signals disagree) flags ``needs_human_review``.
- **Degradation**: an empty/failed source is recorded but never interrupts the flow.
- **Deployment asymmetry**: a recent deploy is an anomaly-correlation signal; the
  *absence* of a deploy is neutral, not a "healthy" dissent (avoids false conflicts).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

# Trace > Metrics > Logs > Git/deployment.
SOURCE_WEIGHTS: dict[str, float] = {
    "traces": 1.0,
    "metrics": 0.8,
    "logs": 0.6,
    "deployment": 0.4,
}

CHANGE_RATIO_THRESHOLD = 0.3
CORROBORATION_MAX_BONUS = 0.15
CONFLICT_PENALTY = 0.2
CONFIDENCE_FLOOR = 0.05
CONFIDENCE_CEIL = 0.99

_LOG_ERROR_TOKENS = (
    "error",
    "exception",
    "timeout",
    "5xx",
    "fail",
    "conn",
    "cache",
    "oom",
    "crash",
)

ANOMALY = "anomaly"
NORMAL = "normal"


@dataclass(frozen=True)
class Signal:
    """A directional signal from one evidence source."""

    source: str
    direction: str  # ANOMALY | NORMAL
    weight: float


def cross_validate_state(state: Mapping[str, Any]) -> dict[str, Any]:
    """Derive signals from agent state evidence and fuse them."""
    signals, degraded = derive_signals(state)
    return cross_validate(signals, degraded)


def derive_signals(state: Mapping[str, Any]) -> tuple[list[Signal], list[str]]:
    """Extract one directional signal per non-degraded evidence source."""
    buckets: dict[str, list[dict[str, Any]]] = {
        "metrics": state.get("metrics_evidence", []) or [],
        "logs": state.get("logs_evidence", []) or [],
        "traces": state.get("traces_evidence", []) or [],
        "deployment": state.get("deployment_evidence", []) or [],
    }
    detectors = {
        "metrics": _metric_direction,
        "logs": _log_direction,
        "traces": _trace_direction,
        "deployment": _deployment_direction,
    }

    signals: list[Signal] = []
    degraded: list[str] = []
    for source, items in buckets.items():
        payloads = [
            item["payload"]
            for item in items
            if isinstance(item, dict) and isinstance(item.get("payload"), dict)
        ]
        if not payloads:
            degraded.append(source)
            continue
        direction = detectors[source](payloads)
        if direction is None:
            continue  # present but neutral — neither corroborates nor conflicts
        signals.append(Signal(source, direction, SOURCE_WEIGHTS[source]))
    return signals, degraded


def cross_validate(
    signals: list[Signal], degraded_sources: list[str] | None = None
) -> dict[str, Any]:
    """Fuse directional signals into a confidence adjustment and review flag."""
    degraded = list(degraded_sources or [])
    anomaly = [s for s in signals if s.direction == ANOMALY]
    normal = [s for s in signals if s.direction == NORMAL]
    anomaly_sources = [s.source for s in anomaly]
    normal_sources = [s.source for s in normal]
    anomaly_weight = round(sum(s.weight for s in anomaly), 4)
    normal_weight = round(sum(s.weight for s in normal), 4)
    notes: list[str] = []

    if not signals:
        status, adjustment, review = "insufficient", 0.0, False
        notes.append("no usable evidence signals; relying on single or absent sources")
    elif anomaly and normal:
        status, review = "conflicting", True
        dissent_share = normal_weight / (anomaly_weight + normal_weight)
        adjustment = -round(CONFLICT_PENALTY * dissent_share, 4)
        notes.append(f"sources disagree: anomaly={anomaly_sources} healthy={normal_sources}")
    elif len(anomaly) >= 2:
        status, review = "corroborated", False
        adjustment = round(min(CORROBORATION_MAX_BONUS, 0.05 * len(anomaly)), 4)
        notes.append(f"{len(anomaly)} sources corroborate: {anomaly_sources}")
    elif len(anomaly) == 1:
        status, adjustment, review = "single_source", 0.0, False
        notes.append(f"single anomaly source: {anomaly_sources}")
    else:  # only healthy signals
        status, adjustment, review = "no_anomaly", 0.0, False
        notes.append(f"sources report healthy: {normal_sources}")

    if degraded:
        notes.append(f"degraded/absent sources: {degraded}")

    return {
        "status": status,
        "confidence_adjustment": adjustment,
        "needs_human_review": review,
        "corroborating_sources": anomaly_sources,
        "healthy_sources": normal_sources,
        "degraded_sources": degraded,
        "weighted_anomaly_score": anomaly_weight,
        "weighted_healthy_score": normal_weight,
        "signal_count": len(signals),
        "notes": notes,
    }


def apply_confidence_adjustment(confidence: float, adjustment: float) -> float:
    """Clamp an adjusted confidence into the allowed range."""
    return round(min(CONFIDENCE_CEIL, max(CONFIDENCE_FLOOR, confidence + adjustment)), 4)


def _metric_direction(payloads: list[dict[str, Any]]) -> str | None:
    stats_list = [p["stats"] for p in payloads if isinstance(p.get("stats"), dict)]
    if not stats_list:
        return None
    for stats in stats_list:
        if abs(_as_float(stats.get("change_ratio"))) >= CHANGE_RATIO_THRESHOLD:
            return ANOMALY
    return NORMAL


def _log_direction(payloads: list[dict[str, Any]]) -> str | None:
    saw_logs = False
    for payload in payloads:
        counts = payload.get("error_type_counts")
        if isinstance(counts, dict):
            saw_logs = True
        if payload.get("sample_count") or payload.get("line_count"):
            saw_logs = True
        top = str(payload.get("top_error_type") or "").lower()
        if top and any(token in top for token in _LOG_ERROR_TOKENS):
            return ANOMALY
        if _as_float(payload.get("error_count")) > 0:
            return ANOMALY
    return NORMAL if saw_logs else None


def _trace_direction(payloads: list[dict[str, Any]]) -> str | None:
    saw_spans = False
    for payload in payloads:
        if _as_float(payload.get("errors")) > 0 or payload.get("error_spans"):
            return ANOMALY
        if payload.get("spans") is not None:
            saw_spans = True
    return NORMAL if saw_spans else None


def _deployment_direction(payloads: list[dict[str, Any]]) -> str | None:
    # A recent deploy correlates with the incident. No deploy is neutral.
    for payload in payloads:
        if _as_float(payload.get("change_count")) > 0 or payload.get("changes"):
            return ANOMALY
    return None


def _as_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
