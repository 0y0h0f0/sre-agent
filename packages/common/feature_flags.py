"""M9 Feature Flag resolution.

Central module for resolving M9 feature flags with priority logic, conflict
detection, and metrics recording.

Principle:
- ``M9_EXTENSIONS_ENABLED=false`` forces ALL M9 sub-capabilities off.
- Sub-feature flags only take effect when the global gate is on.
- Conflicts (sub-feature=true while global=false) produce a warning log and a
  Prometheus metric but never prevent service startup.

Special rule:
- M9 disabled + TRACE_BACKEND=jaeger → keeps M8-verified Jaeger behavior.
  M9 does NOT disable existing Jaeger.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from packages.common.metrics import m9_feature_flag_conflict_total
from packages.common.settings import Settings

logger = logging.getLogger(__name__)

# M9 sub-feature names mapped to their Settings attribute names.
_M9_SUBFEATURE_ATTRS: dict[str, str] = {
    "runbook_llm_generation": "runbook_llm_generation_enabled",
    "llm_incident_diff": "llm_incident_diff_enabled",
    "runbook_web_search": "runbook_web_search_enabled",
    "tempo_discovery": "tempo_discovery_enabled",
    "grafana_alert_ingest": "grafana_alert_ingest_enabled",
    "semantic_runbook_search": "semantic_runbook_search_enabled",
    "external_embedding_provider": "external_embedding_provider_enabled",
}


@dataclass
class FeatureFlagConflict:
    """A single feature flag conflict record."""

    feature: str
    message: str


@dataclass
class M9FeatureFlags:
    """Resolved M9 feature flag state.

    This is the authoritative snapshot of which M9 capabilities are active.
    Consumers should read from this struct rather than checking Settings directly.
    """

    m9_enabled: bool

    # Sub-feature resolved states (all False when m9_enabled is False).
    runbook_llm_generation: bool = False
    llm_incident_diff: bool = False
    runbook_web_search: bool = False
    tempo_discovery: bool = False
    grafana_alert_ingest: bool = False
    semantic_runbook_search: bool = False
    external_embedding_provider: bool = False

    # Trace backend state.
    trace_backend: str = "fixture"
    trace_enabled: bool = False
    tempo_degraded: bool = False

    # List of conflicts detected during resolution.
    conflicts: list[FeatureFlagConflict] = field(default_factory=list)


def is_m9_enabled(settings: Settings) -> bool:
    """Return True if the M9 global feature gate is enabled."""
    return settings.m9_extensions_enabled


def is_m9_subfeature_enabled(settings: Settings, feature_name: str) -> bool:
    """Check whether a specific M9 sub-feature is effectively enabled.

    Returns True only if BOTH the M9 global gate AND the individual sub-feature
    flag are True.  Does NOT record conflicts — use resolve_m9_feature_flags()
    for the full resolution with conflict detection.

    Args:
        settings: Application settings.
        feature_name: Logical feature name (e.g. "runbook_llm_generation").

    Returns:
        True if the feature is effectively enabled.
    """
    if not settings.m9_extensions_enabled:
        return False
    attr = _M9_SUBFEATURE_ATTRS.get(feature_name)
    if attr is None:
        return False
    return bool(getattr(settings, attr, False))


def _resolve_subfeature(
    settings: Settings,
    feature_name: str,
    *,
    m9_enabled: bool,
    conflicts: list[FeatureFlagConflict],
) -> bool:
    """Resolve a single M9 sub-feature.

    Args:
        settings: Application settings.
        feature_name: Logical feature name (key in _M9_SUBFEATURE_ATTRS).
        m9_enabled: Whether the M9 global gate is on.
        conflicts: Accumulator list for conflict records.

    Returns:
        Resolved feature state: True only if both M9 global gate AND the
        individual sub-feature flag are True.
    """
    attr = _M9_SUBFEATURE_ATTRS.get(feature_name)
    if attr is None:
        logger.warning("Unknown M9 sub-feature: %s", feature_name)
        return False

    subfeature_on = getattr(settings, attr, False)

    if not m9_enabled and subfeature_on:
        msg = (
            f"M9 feature '{feature_name}' is enabled in settings but "
            f"M9_EXTENSIONS_ENABLED=false overrides it — feature is DISABLED"
        )
        logger.warning(msg)
        m9_feature_flag_conflict_total.labels(feature=feature_name).inc()
        conflicts.append(FeatureFlagConflict(feature=feature_name, message=msg))
        return False

    if not m9_enabled:
        return False

    return subfeature_on


def resolve_m9_feature_flags(settings: Settings) -> M9FeatureFlags:
    """Resolve all M9 feature flags into a single snapshot.

    This is the primary entry point. It applies the global gate, resolves each
    sub-feature, handles the special Jaeger/Tempo rules, and records conflicts.

    Args:
        settings: Application settings instance.

    Returns:
        M9FeatureFlags with all resolved states and any conflicts detected.
    """
    m9_enabled = is_m9_enabled(settings)
    conflicts: list[FeatureFlagConflict] = []

    flags = M9FeatureFlags(m9_enabled=m9_enabled)

    # Resolve each sub-feature.
    for feature_name in _M9_SUBFEATURE_ATTRS:
        resolved = _resolve_subfeature(
            settings, feature_name, m9_enabled=m9_enabled, conflicts=conflicts
        )
        setattr(flags, feature_name, resolved)

    # Trace backend resolution.
    flags.trace_backend = settings.trace_backend
    flags.trace_enabled = settings.trace_enabled

    # Special rule: M9 disabled + TRACE_BACKEND=jaeger → Jaeger stays active
    # (M8-verified behavior, not an M9 feature).
    # M9 disabled + TRACE_BACKEND=tempo → Tempo is degraded (it IS an M9 feature).
    if not m9_enabled and settings.trace_backend == "tempo":
        flags.tempo_degraded = True
        msg = (
            "TRACE_BACKEND=tempo requires M9_EXTENSIONS_ENABLED=true — "
            "Tempo is an M9 feature and is currently DEGRADED"
        )
        logger.warning(msg)
        m9_feature_flag_conflict_total.labels(feature="tempo_trace_backend").inc()
        conflicts.append(
            FeatureFlagConflict(feature="tempo_trace_backend", message=msg)
        )

    flags.conflicts = conflicts
    return flags
