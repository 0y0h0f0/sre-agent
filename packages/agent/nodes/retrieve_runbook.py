"""Retrieve relevant runbook chunks via RunbookSearchTool."""

from __future__ import annotations

from typing import Any

from packages.agent.nodes._persist import persist_evidence
from packages.agent.schemas import AgentDeps
from packages.agent.state import IncidentState
from packages.common.ids import new_id
from packages.common.time import utc_now
from packages.tools.runbook_search import RunbookSearchQuery


def retrieve_runbook(state: IncidentState, deps: AgentDeps) -> IncidentState:
    node_id = new_id("nd_")
    started_at = utc_now()
    try:
        service = state["service_name"]
        alert_name = state["alert_name"]
        query = RunbookSearchQuery(query=alert_name, service=service, top_k=5)
        result = deps.runbook_search_tool.run(query)
        deps.tool_call_recorder(
            agent_run_id=state["agent_run_id"],
            node_name="retrieve_runbook",
            tool_name=deps.runbook_search_tool.name,
            query=query,
            result=result,
            input_summary=f"q={alert_name} service={service}",
        )
        raw_chunks = result.data.get("results", []) if isinstance(result.data, dict) else []
        chunks = (
            [dict(chunk) for chunk in raw_chunks if isinstance(chunk, dict)]
            if isinstance(raw_chunks, list)
            else []
        )
        runbook_evidence = _runbook_evidence_items(result.evidence)
        if runbook_evidence:
            persisted = persist_evidence(
                deps.db,
                state["incident_id"],
                state["agent_run_id"],
                runbook_evidence,
            )
            chunks = _attach_evidence_refs(chunks, persisted)
        deps.node_tracer(
            node_id=node_id,
            agent_run_id=state["agent_run_id"],
            name="retrieve_runbook",
            status=result.status,
            started_at=started_at,
            finished_at=utc_now(),
            input_summary=f"query={alert_name}",
            output_summary=f"found {len(chunks)} chunks",
        )
        return {**state, "runbook_context": chunks, "phase": "runbook_retrieved"}
    except Exception as exc:
        deps.node_tracer(
            node_id=node_id,
            agent_run_id=state["agent_run_id"],
            name="retrieve_runbook",
            status="failed",
            started_at=started_at,
            finished_at=utc_now(),
            error_message=str(exc),
        )
        state.setdefault("errors", []).append({"node": "retrieve_runbook", "error": str(exc)})
        return state


def _runbook_evidence_items(evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        dict(item)
        for item in evidence
        if isinstance(item, dict) and item.get("type") == "runbook"
    ]


def _attach_evidence_refs(
    chunks: list[dict[str, Any]], evidence: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    evidence_by_chunk = {
        chunk_id: item
        for item in evidence
        if (chunk_id := _evidence_chunk_id(item))
    }
    enriched: list[dict[str, Any]] = []
    for chunk in chunks:
        if not isinstance(chunk, dict):
            continue
        chunk_copy = dict(chunk)
        chunk_id = str(chunk_copy.get("chunk_id", ""))
        match = evidence_by_chunk.get(chunk_id)
        if match:
            chunk_copy["evidence_id"] = match.get("evidence_id")
            chunk_copy["source_id"] = match.get("source_id") or chunk_id
            chunk_copy["evidence_source"] = "runbook"
        enriched.append(chunk_copy)
    return enriched


def _evidence_chunk_id(item: dict[str, Any]) -> str:
    payload = item.get("payload")
    payload_dict = payload if isinstance(payload, dict) else {}
    value = item.get("source_id") or payload_dict.get("chunk_id")
    return str(value) if value else ""
