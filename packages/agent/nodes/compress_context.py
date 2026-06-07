"""Compress oversized context into summary memories (Claude Code style).

Claude Code compresses conversation history by summarizing older messages
into a structured preamble. This node does the same for diagnosis context:
when evidence exceeds the token budget, the compressor reduces detail and
writes a compressed summary to memory for future retrieval.

The compressed memory is written to L2 (service scope) so it is available
when a similar incident occurs — analogous to how Claude Code injects the
compressed summary at the start of the next turn.
"""

from __future__ import annotations

import json
from typing import Any

from packages.agent.schemas import AgentDeps
from packages.agent.state import IncidentState
from packages.common.ids import new_id
from packages.common.time import utc_now
from packages.memory.schemas import MemoryItemCreate


def compress_context(state: IncidentState, deps: AgentDeps) -> IncidentState:
    """Compress oversized evidence blocks into summary memories.

    Inserted after ``diagnose`` in the graph. If compression events were
    recorded during ``build_context``, the compressed summaries are
    persisted as service-scope semantic memories so they are reusable
    across future incidents of the same type.

    This implements the Claude Code pattern: when context exceeds budget,
    summarize the excess into a retrievable memory rather than silently
    dropping it.
    """
    node_id = new_id("nd_")
    started_at = utc_now()
    try:
        agent_run_id = state.get("agent_run_id", "")
        service = state.get("service_name", "")
        alert_name = state.get("alert_name", "")
        incident_id = state.get("incident_id", "")
        compression_events = state.get("compression_events", [])

        if not compression_events or not service:
            deps.node_tracer(
                node_id=node_id, agent_run_id=agent_run_id,
                name="compress_context", status="succeeded",
                started_at=started_at, finished_at=utc_now(),
                input_summary="skipped: no compression events or service",
            )
            return state

        written = 0
        for event in compression_events:
            summary = event.get("summary", "")
            omitted = event.get("omitted_evidence_ids", [])
            retained = event.get("retained_evidence_ids", [])
            before_tokens = event.get("before_tokens", 0)
            after_tokens = event.get("after_tokens", 0)
            ratio = event.get("compression_ratio", 1.0)
            risk_notes = event.get("risk_notes", [])

            content = json.dumps({
                "alert_name": alert_name,
                "summary": summary,
                "omitted_evidence_count": len(omitted),
                "retained_evidence_count": len(retained),
                "before_tokens": before_tokens,
                "after_tokens": after_tokens,
                "compression_ratio": round(ratio, 3),
                "risk_notes": risk_notes,
            }, default=str)

            deps.memory_store.put(MemoryItemCreate(
                scope="service",
                scope_key=service,
                memory_type="semantic",
                content=content,
                content_json={
                    "service": service,
                    "alert_name": alert_name,
                    "compression_event": True,
                    "omitted_count": len(omitted),
                },
                importance=max(0.3, 1.0 - ratio),
                source_ref=f"incident:{incident_id}",
            ))
            written += 1

        deps.node_tracer(
            node_id=node_id, agent_run_id=agent_run_id,
            name="compress_context", status="succeeded",
            started_at=started_at, finished_at=utc_now(),
            input_summary=f"compression_events={len(compression_events)}",
            output_summary=f"wrote {written} compressed memory entries",
        )
    except Exception as exc:
        deps.node_tracer(
            node_id=node_id, agent_run_id=state.get("agent_run_id", ""),
            name="compress_context", status="failed",
            started_at=started_at, finished_at=utc_now(),
            error_message=str(exc),
        )
        state.setdefault("errors", []).append(
            {"node": "compress_context", "error": str(exc)}
        )
    return state
