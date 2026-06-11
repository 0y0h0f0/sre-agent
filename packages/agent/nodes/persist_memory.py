"""Persist diagnosis results to memory_items (Phase 5 memory write port).

Writes structured memories after diagnosis completes so that future
incidents can reuse conclusions and action patterns.

Claude Code parallel: after each conversation turn, key facts/decisions
are persisted to memory so they survive context eviction.
"""

from __future__ import annotations

import json

from packages.agent.schemas import AgentDeps
from packages.agent.state import IncidentState
from packages.common.ids import new_id
from packages.common.time import utc_now
from packages.memory.schemas import MemoryItemCreate


def persist_memory(state: IncidentState, deps: AgentDeps) -> IncidentState:
    """Write diagnosis outcome to multi-level memory.

    Inserted after ``generate_report`` in the graph. Failures here
    are logged but never abort the pipeline — memory is best-effort.
    """
    node_id = new_id("nd_")
    started_at = utc_now()
    try:
        incident_id = state.get("incident_id", "")
        agent_run_id = state.get("agent_run_id", "")
        service = state.get("service_name", "")
        alert_name = state.get("alert_name", "")
        fingerprint = state.get("alert_payload", {}).get("fingerprint", "")

        if not incident_id or not service:
            deps.node_tracer(
                node_id=node_id, agent_run_id=agent_run_id,
                name="persist_memory", status="succeeded",
                started_at=started_at, finished_at=utc_now(),
                input_summary="skipped: no incident_id or service",
            )
            return state

        root_cause = state.get("root_cause", {})
        report = state.get("incident_report", {})
        actions = state.get("recommended_actions", [])
        compression_events = state.get("compression_events", [])

        # ---- L1: incident-scope episodic memory ----
        l1_content = json.dumps({
            "alert_name": alert_name,
            "fingerprint": fingerprint,
            "root_cause": root_cause.get("summary", ""),
            "confidence": root_cause.get("confidence"),
            "actions_count": len(actions),
            "compression_triggered": len(compression_events) > 0,
        }, default=str)
        deps.memory_store.put(MemoryItemCreate(
            scope="incident",
            scope_key=incident_id,
            memory_type="episodic",
            content=l1_content,
            importance=0.8,
            source_ref=f"agent_run:{agent_run_id}",
        ))

        # ---- L2: service-scope semantic memory ----
        if root_cause.get("summary"):
            l2_content = json.dumps({
                "alert_name": alert_name,
                "fingerprint": fingerprint,
                "root_cause": root_cause.get("summary"),
                "confidence": root_cause.get("confidence"),
                "evidence_ids": root_cause.get("evidence_ids", []),
                "report_summary": (
                    report.get("root_cause", "")[:500]
                    if isinstance(report, dict) else ""
                ),
            }, default=str)
            deps.memory_store.put(MemoryItemCreate(
                scope="service",
                scope_key=service,
                memory_type="semantic",
                content=l2_content,
                content_json={"service": service, "fingerprint": fingerprint},
                importance=0.6,
                source_ref=f"incident:{incident_id}",
            ))

        # ---- L3: procedural memory from successful actions ----
        executed = [
            a for a in actions
            if a.get("status") in ("succeeded", "executed", "approved")
            and a.get("risk_level", "L4") in ("L0", "L1", "L2")
        ]
        for action in executed[:3]:
            l3_content = json.dumps({
                "type": action.get("type", ""),
                "target": action.get("target", ""),
                "reason": action.get("reason", ""),
                "risk_level": action.get("risk_level", ""),
                "service": service,
                "alert_name": alert_name,
            }, default=str)
            deps.memory_store.put(MemoryItemCreate(
                scope="global",
                scope_key=f"action:{action.get('type', 'unknown')}",
                memory_type="procedural",
                content=l3_content,
                content_json={"service": service},
                importance=0.5,
                source_ref=f"incident:{incident_id}",
            ))

        # ---- L0: run-local context surviving write ----
        deps.memory_store.put(MemoryItemCreate(
            scope="run",
            scope_key=agent_run_id,
            memory_type="episodic",
            content=json.dumps({
                "phase": state.get("phase", ""),
                "diagnosis_complete": True,
                "timing": {
                    "started_at": state.get("time_window", {}).get("start"),
                },
            }, default=str),
            importance=0.3,
            expires_at=None,  # no expiry; cleaned up by scope-limit on retrieval
            source_ref=f"agent_run:{agent_run_id}",
        ))

        deps.node_tracer(
            node_id=node_id, agent_run_id=agent_run_id,
            name="persist_memory", status="succeeded",
            started_at=started_at, finished_at=utc_now(),
            input_summary=f"service={service} alert={alert_name}",
            output_summary=f"wrote L0+L1+L2+{min(len(executed), 3)}xL3 memories",
        )
    except Exception as exc:
        deps.node_tracer(
            node_id=node_id, agent_run_id=state.get("agent_run_id", ""),
            name="persist_memory", status="failed",
            started_at=started_at, finished_at=utc_now(),
            error_message=str(exc),
        )
        state.setdefault("errors", []).append(
            {"node": "persist_memory", "error": str(exc)}
        )
    return state
