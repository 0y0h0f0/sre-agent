"""Cross-incident association node (Phase 5.1).

Finds related past incidents by fingerprint and service similarity,
then enriches state with their diagnoses for downstream nodes.
"""

from __future__ import annotations

from typing import Any

from packages.agent.schemas import AgentDeps
from packages.agent.state import IncidentState
from packages.db.repositories.incident_correlations import IncidentCorrelationRepository
from packages.db.repositories.incidents import IncidentRepository


def cross_incident(state: IncidentState, deps: AgentDeps) -> IncidentState:
    """Find related past incidents and populate cross_incident_context.

    Inserts between ``retrieve_memory`` and ``retrieve_runbook`` in the graph.
    """
    import logging

    from packages.common.ids import new_id
    from packages.common.time import utc_now

    logger = logging.getLogger(__name__)
    incident_id = state.get("incident_id", "")
    agent_run_id = state.get("agent_run_id", "")
    fingerprint = state.get("alert_payload", {}).get("fingerprint", "")
    service_name = state.get("service_name", "")
    node_id = new_id("nd_")
    started_at = utc_now()

    if not incident_id or not service_name or not fingerprint:
        deps.node_tracer(
            node_id=node_id, agent_run_id=agent_run_id,
            name="cross_incident", status="skipped",
            started_at=started_at, finished_at=utc_now(),
            input_summary=f"incident={incident_id} service={service_name}",
            output_summary="skipped: missing incident_id, service_name, or fingerprint",
        )
        return {**state, "cross_incident_context": []}

    try:
        incidents_repo = IncidentRepository(deps.db)
        correlations_repo = IncidentCorrelationRepository(deps.db)
        settings = deps.settings
        max_results = settings.cross_incident_max_results
        context: list[dict[str, Any]] = []

        # 1. Same-fingerprint matches
        same_fp = correlations_repo.find_by_fingerprint(
            fingerprint, exclude_incident_id=incident_id, limit=max_results
        )
        for related in same_fp:
            context.append(
                {
                    "incident_id": related.incident_id,
                    "service": related.service,
                    "alert_name": related.alert_name,
                    "severity": related.severity,
                    "root_cause_summary": related.root_cause_summary,
                    "status": related.status,
                    "correlation_type": "same_fingerprint",
                    "created_at": related.created_at.isoformat() if related.created_at else None,
                }
            )
            try:
                correlations_repo.create(
                    incident_id_a=incident_id,
                    incident_id_b=related.incident_id,
                    correlation_type="same_fingerprint",
                )
            except Exception:
                logger.debug(
                    "failed to record same_fingerprint correlation %s<->%s",
                    incident_id, related.incident_id, exc_info=True,
                )

        # 2. Same-service matches (fill up to max_results)
        if len(context) < max_results:
            same_svc = correlations_repo.find_similar_by_service(
                service_name, exclude_incident_id=incident_id, limit=max_results
            )
            seen = {c["incident_id"] for c in context}
            for related in same_svc:
                if related.incident_id in seen:
                    continue
                context.append(
                    {
                        "incident_id": related.incident_id,
                        "service": related.service,
                        "alert_name": related.alert_name,
                        "severity": related.severity,
                        "root_cause_summary": related.root_cause_summary,
                        "status": related.status,
                        "correlation_type": "similar_service",
                        "created_at": related.created_at.isoformat() if related.created_at else None,
                    }
                )
                seen.add(related.incident_id)
                try:
                    correlations_repo.create(
                        incident_id_a=incident_id,
                        incident_id_b=related.incident_id,
                        correlation_type="similar_service",
                    )
                except Exception:
                    logger.debug(
                        "failed to record similar_service correlation %s<->%s",
                        incident_id, related.incident_id, exc_info=True,
                    )
                if len(context) >= max_results:
                    break

        deps.node_tracer(
            node_id=node_id, agent_run_id=agent_run_id,
            name="cross_incident", status="succeeded",
            started_at=started_at, finished_at=utc_now(),
            input_summary=f"service={service_name} fingerprint={fingerprint[:20]}",
            output_summary=f"found {len(context)} related incidents",
        )
        return {**state, "cross_incident_context": context}

    except Exception as exc:
        logger.error("cross_incident failed: %s", exc, exc_info=True)
        deps.node_tracer(
            node_id=node_id, agent_run_id=agent_run_id,
            name="cross_incident", status="failed",
            started_at=started_at, finished_at=utc_now(),
            input_summary=f"service={service_name}",
            error_message=str(exc)[:500],
        )
        error_record = {
            "node": "cross_incident",
            "error": str(exc),
            "incident_id": incident_id,
        }
        return {
            **state,
            "cross_incident_context": [],
            "errors": state.get("errors", []) + [error_record],
        }
