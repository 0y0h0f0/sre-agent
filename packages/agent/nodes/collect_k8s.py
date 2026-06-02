"""Collect Kubernetes read-only diagnosis evidence (roadmap Phase 2.2).

No-op when ``deps.k8s_tool`` is absent (e.g. the eval harness), so the graph
stays valid and deterministic tests are unaffected.
"""

from __future__ import annotations

from packages.agent.nodes._persist import persist_evidence
from packages.agent.schemas import AgentDeps
from packages.agent.state import IncidentState
from packages.common.ids import new_id
from packages.common.time import utc_now
from packages.tools.k8s import K8sQuery

# Fault classes that implicate the compute/pod layer. Querying K8s only when the
# fault plausibly involves it keeps irrelevant pod state out of the evidence the
# diagnosis (and cross-validation) reasons over.
_K8S_KEYWORDS = (
    "pod",
    "restart",
    "oom",
    "memory",
    "cpu",
    "throttl",
    "crash",
    "image",
    "probe",
    "node",
    "dns",
    "outage",
)
_TOP_SEVERITIES = {"P0", "SEV1", "CRITICAL"}


def _k8s_relevant(alert_name: str, severity: str) -> bool:
    if severity.strip().upper() in _TOP_SEVERITIES:
        return True
    name = alert_name.lower()
    return any(keyword in name for keyword in _K8S_KEYWORDS)


def collect_k8s(state: IncidentState, deps: AgentDeps) -> IncidentState:
    if deps.k8s_tool is None or not _k8s_relevant(
        state.get("alert_name", ""), state.get("severity", "")
    ):
        return {**state, "k8s_evidence": [], "phase": "k8s_collected"}

    node_id = new_id("nd_")
    started_at = utc_now()
    try:
        service = state.get("service_name", "unknown")
        namespace = deps.settings.k8s_namespace
        query = K8sQuery(service=service, operation="events", namespace=namespace)
        result = deps.k8s_tool.run(query)
        deps.tool_call_recorder(
            agent_run_id=state["agent_run_id"],
            node_name="collect_k8s",
            tool_name=deps.k8s_tool.name,
            query=query,
            result=result,
            input_summary=f"service={service} op=events",
        )
        evidence = (
            result.evidence
            if result.evidence
            else [
                {
                    "type": "k8s",
                    "source": "k8s",
                    "service": service,
                    "status": result.status,
                    "summary": result.summary,
                }
            ]
        )
        evidence = persist_evidence(
            deps.db, state["incident_id"], state["agent_run_id"], evidence
        )
        deps.node_tracer(
            node_id=node_id,
            agent_run_id=state["agent_run_id"],
            name="collect_k8s",
            status=result.status,
            started_at=started_at,
            finished_at=utc_now(),
            input_summary=f"service={service}",
            output_summary=result.summary,
        )
        return {**state, "k8s_evidence": evidence, "phase": "k8s_collected"}
    except Exception as exc:
        deps.node_tracer(
            node_id=node_id,
            agent_run_id=state["agent_run_id"],
            name="collect_k8s",
            status="failed",
            started_at=started_at,
            finished_at=utc_now(),
            error_message=str(exc),
        )
        state.setdefault("errors", []).append({"node": "collect_k8s", "error": str(exc)})
        return state
