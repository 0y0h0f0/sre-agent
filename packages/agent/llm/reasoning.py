"""Reasoning-depth layering and LLM-call auditing (roadmap Phase 1.2).

Core diagnosis nodes run with deep reasoning (thinking/extended thinking) while
the rest stay on standard inference. The selection is config-driven so it can be
switched off entirely for fast local iteration.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from packages.agent.llm.base import LLMCallMetadata
from packages.agent.llm.profiles import resolve_llm_profile
from packages.agent.state import IncidentState
from packages.common.settings import Settings

# Nodes that benefit from deep reasoning by default. Only LLM-calling nodes are
# meaningful here; ``diagnose`` is the core hypothesis/root-cause reasoner.
DEFAULT_DEEP_REASONING_NODES = frozenset({"diagnose", "diagnose_synthesize"})
_TOP_REASONING_SEVERITIES = frozenset({"P0", "SEV0", "SEV1", "CRITICAL"})

_SAFE_USAGE_FIELDS = frozenset(
    {
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "cached_prompt_tokens",
        "reasoning_tokens",
    }
)
_SAFE_PROVIDER_CACHE_STATUSES = frozenset({"hit", "miss", "unknown"})


def deep_reasoning_nodes(settings: Settings) -> frozenset[str]:
    """Resolve the set of node names configured for deep reasoning."""
    raw = (settings.llm_reasoning_nodes or "").strip()
    if not raw:
        return DEFAULT_DEEP_REASONING_NODES
    return frozenset(name.strip() for name in raw.split(",") if name.strip())


def should_use_deep_reasoning(settings: Settings, node_name: str) -> bool:
    """Whether ``node_name`` should request deep reasoning for this run.

    The master switch ``llm_reasoning_enabled`` gates everything so developers
    can disable thinking globally to speed up iteration.
    """
    if not settings.llm_reasoning_enabled:
        return False
    return node_name in deep_reasoning_nodes(settings)


def should_use_diagnosis_reasoning(
    settings: Settings,
    node_name: str,
    state: Mapping[str, Any],
    *,
    cross_validation: Mapping[str, Any] | None = None,
    cascade_analysis: Mapping[str, Any] | None = None,
) -> bool:
    """Gate diagnosis thinking on config plus a concrete complexity trigger."""

    if not should_use_deep_reasoning(settings, node_name):
        return False
    if _operator_forced_reasoning(settings, node_name):
        return True
    return bool(
        diagnosis_reasoning_trigger(
            state,
            cross_validation=cross_validation,
            cascade_analysis=cascade_analysis,
        )
    )


def diagnosis_reasoning_trigger(
    state: Mapping[str, Any],
    *,
    cross_validation: Mapping[str, Any] | None = None,
    cascade_analysis: Mapping[str, Any] | None = None,
) -> str:
    """Return the low-cardinality reason that justifies deep diagnosis."""

    validation = cross_validation or _mapping_value(state.get("cross_validation"))
    if validation and (
        validation.get("needs_human_review") is True
        or validation.get("status") == "conflicting"
    ):
        return "evidence_conflict"

    severity = str(state.get("severity", "") or "").strip().upper()
    if severity in _TOP_REASONING_SEVERITIES:
        return "top_severity"

    cascade = cascade_analysis or _mapping_value(state.get("cascade_analysis"))
    if cascade and cascade.get("is_cascade") is True:
        return "cascade_suspicion"

    if _has_missing_evidence(state):
        return "missing_evidence"

    return ""


def llm_profile_call_options(
    settings: Settings,
    profile: str,
    *,
    aliases: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Return per-call overrides only when a profile differs from global defaults."""

    resolved = resolve_llm_profile(settings, profile, aliases=aliases)
    options: dict[str, Any] = {}
    if resolved.model != settings.llm_model:
        options["model"] = resolved.model
    if resolved.max_tokens != settings.llm_max_tokens:
        options["max_tokens"] = resolved.max_tokens
    if resolved.temperature != settings.llm_temperature:
        options["temperature"] = resolved.temperature
    if resolved.reasoning_effort != settings.llm_reasoning_effort:
        options["reasoning_effort"] = resolved.reasoning_effort
    return options


def capture_metadata(llm: Any) -> dict[str, Any]:
    """Snapshot the adapter's last-call metadata, tolerating plain LLMs."""
    meta = getattr(llm, "last_metadata", None)
    return dict(meta) if meta else {}


def format_call_metadata(meta: LLMCallMetadata | dict[str, Any] | None) -> str:
    """Compact, audit-friendly one-liner for node trace summaries."""
    if not meta:
        return ""
    safe_meta = _safe_llm_call_metadata(dict(meta))
    if not safe_meta:
        return ""
    parts: list[str] = []
    provider = safe_meta.get("provider", "")
    model = safe_meta.get("model", "")
    if provider or model:
        parts.append(f"llm={provider}/{model}")
    usage = safe_meta.get("usage") or {}
    if usage:
        parts.append(f"tok={usage.get('prompt_tokens', 0)}/{usage.get('completion_tokens', 0)}")
    if "redaction_count" in safe_meta:
        parts.append(f"redact={safe_meta.get('redaction_count', 0)}")
    return " ".join(parts)


def record_llm_call(state: IncidentState, node_name: str, meta: dict[str, Any]) -> None:
    """Append a structured LLM-call record to state for auditability.

    Stores an explicit allowlist of provider/model/token/cache/latency/redaction
    fields. Raw prompt, completion, query, response, and reasoning fields are
    intentionally not accepted.

    Thread-safe via :class:`threading.Lock` for parallel node execution.
    """
    if not meta:
        return
    safe_meta = _safe_llm_call_metadata(meta)
    if not safe_meta:
        return
    record = {"node": node_name, **safe_meta}
    state.setdefault("llm_calls", []).append(record)


def _safe_llm_call_metadata(meta: dict[str, Any]) -> dict[str, Any]:
    safe: dict[str, Any] = {}

    for key in ("provider", "model", "finish_reason", "service_tier"):
        value = meta.get(key)
        if isinstance(value, str) and value:
            safe[key] = value

    usage = _safe_usage(meta.get("usage"))
    if usage:
        safe["usage"] = usage

    provider_cache_status = meta.get("provider_cache_status")
    if (
        isinstance(provider_cache_status, str)
        and provider_cache_status in _SAFE_PROVIDER_CACHE_STATUSES
    ):
        safe["provider_cache_status"] = provider_cache_status
        if provider_cache_status in {"hit", "miss"}:
            # Temporary compatibility for current worker aggregation. The
            # tri-state field remains canonical; unknown is intentionally not
            # folded into a legacy boolean.
            safe["cache_hit"] = provider_cache_status == "hit"

    cache_hit = meta.get("cache_hit")
    if (
        "provider_cache_status" not in safe
        and "cache_hit" not in safe
        and isinstance(cache_hit, bool)
    ):
        safe["cache_hit"] = cache_hit

    duration_ms = _safe_non_negative_int(meta.get("duration_ms"))
    if duration_ms is not None:
        safe["duration_ms"] = duration_ms

    redaction_applied = meta.get("redaction_applied")
    if isinstance(redaction_applied, bool):
        safe["redaction_applied"] = redaction_applied

    redaction_count = _safe_non_negative_int(meta.get("redaction_count"))
    if redaction_count is not None:
        safe["redaction_count"] = redaction_count

    redaction_types = meta.get("redaction_types")
    if isinstance(redaction_types, list):
        filtered = sorted({item for item in redaction_types if isinstance(item, str) and item})
        if filtered:
            safe["redaction_types"] = filtered

    return safe


def _safe_usage(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    safe: dict[str, int] = {}
    for key in _SAFE_USAGE_FIELDS:
        token_count = _safe_non_negative_int(value.get(key))
        if token_count is not None:
            safe[key] = token_count
    return safe


def _safe_non_negative_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, int | float):
        if value < 0:
            return None
        return int(value)
    return None


def _operator_forced_reasoning(settings: Settings, node_name: str) -> bool:
    raw = (settings.llm_reasoning_nodes or "").strip()
    if not raw:
        return False
    configured = deep_reasoning_nodes(settings)
    return configured != DEFAULT_DEEP_REASONING_NODES and node_name in configured


def _mapping_value(value: Any) -> Mapping[str, Any] | None:
    return value if isinstance(value, Mapping) else None


def _has_missing_evidence(state: Mapping[str, Any]) -> bool:
    rationale = _mapping_value(state.get("diagnosis_rationale"))
    candidates = []
    if rationale:
        candidates.append(rationale.get("missing_evidence"))
    candidates.append(state.get("missing_evidence"))
    for value in candidates:
        if isinstance(value, list | tuple | set) and any(str(item).strip() for item in value):
            return True
        if isinstance(value, str) and value.strip():
            return True
    return False
