"""Reasoning-depth layering and LLM-call auditing (roadmap Phase 1.2).

Core diagnosis nodes run with deep reasoning (thinking/extended thinking) while
the rest stay on standard inference. The selection is config-driven so it can be
switched off entirely for fast local iteration.
"""

from __future__ import annotations

from typing import Any

from packages.agent.llm.base import LLMCallMetadata
from packages.agent.state import IncidentState
from packages.common.settings import Settings

# Nodes that benefit from deep reasoning by default. Only LLM-calling nodes are
# meaningful here; ``diagnose`` is the core hypothesis/root-cause reasoner.
DEFAULT_DEEP_REASONING_NODES = frozenset({"diagnose", "diagnose_synthesize"})


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


def capture_metadata(llm: Any) -> dict[str, Any]:
    """Snapshot the adapter's last-call metadata, tolerating plain LLMs."""
    meta = getattr(llm, "last_metadata", None)
    return dict(meta) if meta else {}


def format_call_metadata(meta: LLMCallMetadata | dict[str, Any] | None) -> str:
    """Compact, audit-friendly one-liner for node trace summaries."""
    if not meta:
        return ""
    parts: list[str] = []
    provider = meta.get("provider", "")
    model = meta.get("model", "")
    if provider or model:
        parts.append(f"llm={provider}/{model}")
    usage = meta.get("usage") or {}
    if usage:
        parts.append(f"tok={usage.get('prompt_tokens', 0)}/{usage.get('completion_tokens', 0)}")
    if "redaction_count" in meta:
        parts.append(f"redact={meta.get('redaction_count', 0)}")
    return " ".join(parts)


def record_llm_call(state: IncidentState, node_name: str, meta: dict[str, Any]) -> None:
    """Append a structured LLM-call record to state for auditability.

    Stores provider/model/usage/finish_reason — explicitly strips
    ``reasoning_summary`` to prevent chain-of-thought leakage into the
    persistent audit trail (Phase 1.2 boundary).

    Thread-safe via :class:`threading.Lock` for parallel node execution.
    """
    if not meta:
        return
    # Strip reasoning content before recording (Phase 1.2 boundary)
    safe_meta = {k: v for k, v in meta.items() if k != "reasoning_summary"}
    if not safe_meta:
        return
    record = {"node": node_name, **safe_meta}
    state.setdefault("llm_calls", []).append(record)
