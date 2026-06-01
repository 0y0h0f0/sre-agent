"""Retrieve relevant memories across L0-L3 scopes."""

from __future__ import annotations

from typing import Any

from packages.agent.schemas import AgentDeps
from packages.agent.state import IncidentState
from packages.common.ids import new_id
from packages.common.time import utc_now
from packages.memory.schemas import MemoryFilters


def retrieve_memory(state: IncidentState, deps: AgentDeps) -> IncidentState:
    node_id = new_id("nd_")
    started_at = utc_now()
    try:
        incident_id = state["incident_id"]
        agent_run_id = state["agent_run_id"]
        service = state["service_name"]
        alert_name = state["alert_name"]
        memories: list[dict[str, Any]] = []

        # L0 run-local
        for m in deps.memory_store.get_by_scope("run", agent_run_id, limit=5):
            memories.append(_serialize(m))
        # L1 incident
        for m in deps.memory_store.get_by_scope("incident", incident_id, limit=5):
            memories.append(_serialize(m))
        # L2 service (vector)
        for m in deps.memory_store.search(
            alert_name, MemoryFilters(scope="service", service=service), top_k=5
        ):
            memories.append(_serialize(m))
        # L3 procedural
        for m in deps.memory_store.search(
            alert_name, MemoryFilters(scope="global", memory_type="procedural"), top_k=3
        ):
            memories.append(_serialize(m))

        deps.node_tracer(
            node_id=node_id,
            agent_run_id=state["agent_run_id"],
            name="retrieve_memory",
            status="succeeded",
            started_at=started_at,
            finished_at=utc_now(),
            input_summary="scopes=run/incident/service/global",
            output_summary=f"found {len(memories)} memories",
        )
        return {**state, "memory_context": memories, "phase": "memory_retrieved"}
    except Exception as exc:
        deps.node_tracer(
            node_id=node_id,
            agent_run_id=state["agent_run_id"],
            name="retrieve_memory",
            status="failed",
            started_at=started_at,
            finished_at=utc_now(),
            error_message=str(exc),
        )
        state.setdefault("errors", []).append({"node": "retrieve_memory", "error": str(exc)})
        return state


def _serialize(m: Any) -> dict[str, Any]:
    return {
        "memory_id": getattr(m, "memory_id", ""),
        "scope": getattr(m, "scope", ""),
        "memory_type": getattr(m, "memory_type", ""),
        "content": getattr(m, "content", ""),
        "importance": getattr(m, "importance", 0.5),
        "source_ref": getattr(m, "source_ref", ""),
    }
