"""Verify that executed actions resolved the incident (ReAct Loop A).

Dispatches deterministic read-only verify gates after L2/L3 actions execute.
The default gate re-queries metrics and logs; Kubernetes and database gates are
selected from action capability metadata when present.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from typing import Any

from packages.agent.actions.capabilities import get_action_capability
from packages.agent.nodes._k8s_targeting import effective_executor_k8s_namespace
from packages.agent.nodes._persist import persist_evidence
from packages.agent.schemas import AgentDeps
from packages.agent.state import IncidentState
from packages.common.ids import new_id
from packages.common.time import utc_now
from packages.tools.db_diagnostics import DbDiagnosticsQuery
from packages.tools.executor_backends import canonical_action_type
from packages.tools.k8s import K8sQuery
from packages.tools.logs import LogsQuery
from packages.tools.metrics import MetricsQuery, MetricType

logger = logging.getLogger(__name__)

MAX_VERIFY_CYCLES = 2

# Thresholds for deterministic before/after comparison.
_ERROR_RATE_RESOLVED = 0.01  # < 1% error rate -> resolved
_ERROR_RATE_IMPROVED = 0.5  # > 50% drop -> improving
_LATENCY_IMPROVED_MS = 0.5  # > 50% latency drop -> improving
_LATENCY_RESOLVED_MS = 100  # < 100ms -> resolved
_DB_CONNECTION_RESOLVED = 50
_DB_CONNECTION_IMPROVED = 0.3
_DEFAULT_VERIFY_GATES = ("metrics_logs",)
_OPTIONAL_VERIFY_GATES = {"db_readonly"}


def verify(state: IncidentState, deps: AgentDeps) -> IncidentState:
    """Check whether executed L2/L3 actions resolved the incident.

    Skips verification when no L2/L3 actions were executed (L0/L1 only).
    Re-queries the original alert's metric type and logs, then compares
    against pre-execution evidence to determine if the issue is resolved,
    improving, or unchanged.
    """
    node_id = new_id("nd_")
    started_at = utc_now()
    try:
        execution_results = state.get("execution_results", [])
        # Only L2/L3 remediations need post-action verification. L0/L1 actions
        # are record/local work and should not create a remediation feedback loop.
        actionable = [
            r for r in execution_results if str(r.get("risk_level", "")).upper() in ("L2", "L3")
        ]
        cycles = int(state.get("_verify_cycles", 0))

        if not actionable or cycles >= MAX_VERIFY_CYCLES:
            # L0/L1 only or hit cycle limit — use "skipped" for no-remediation
            # cases and "resolved" for max-cycles to terminate the loop.
            verdict = "resolved" if cycles >= MAX_VERIFY_CYCLES else "skipped"
            deps.node_tracer(
                node_id=node_id,
                agent_run_id=state["agent_run_id"],
                name="verify",
                status="succeeded",
                started_at=started_at,
                finished_at=utc_now(),
                input_summary=(f"actionable_l23={len(actionable)} cycles={cycles}"),
                output_summary="skipped" if not actionable else "max_cycles",
            )
            return {
                **state,
                "verify_result": verdict,
                "verify_evidence": [],
                "phase": "verified",
            }  # type: ignore[typeddict-unknown-key]

        # Build from capability metadata attached during execution. This avoids
        # trusting free-form model text to decide which verification checks run.
        gate_plan = _build_gate_plan(actionable)

        # Wait for the system to stabilise after the action.
        # NOTE: blocks the Celery worker; acceptable for current scale.
        time.sleep(5)

        service = state.get("service_name", "unknown")
        alert_name = state.get("alert_name", "UnknownAlert")
        # Extend the window to cover "now" — the original window ended at
        # alert time, but we need fresh data post-execution.
        now = utc_now()
        fresh_start = now - timedelta(minutes=5)
        fresh_end = now

        agent_run_id = state["agent_run_id"]
        gate_results: list[dict[str, Any]] = []
        fresh_evidence: list[dict[str, Any]] = []
        for gate in gate_plan:
            result = _run_verify_gate(
                gate,
                state=state,
                deps=deps,
                agent_run_id=agent_run_id,
                service=service,
                alert_name=alert_name,
                fresh_start=fresh_start,
                fresh_end=fresh_end,
            )
            gate_evidence = result.pop("_evidence", [])
            if gate_evidence:
                # Verify observations become first-class evidence so reports and
                # replans can cite concrete post-action facts, not just verdicts.
                gate_evidence = persist_evidence(
                    deps.db,
                    state["incident_id"],
                    state["agent_run_id"],
                    gate_evidence,
                )
                result["evidence_ids"] = [
                    item["evidence_id"] for item in gate_evidence if item.get("evidence_id")
                ]
                fresh_evidence.extend(gate_evidence)
            else:
                result["evidence_ids"] = []
            gate_results.append(result)

        verdict = _combine_gate_verdicts(gate_results)

        deps.node_tracer(
            node_id=node_id,
            agent_run_id=state["agent_run_id"],
            name="verify",
            status="succeeded",
            started_at=started_at,
            finished_at=utc_now(),
            input_summary=f"actions={len(actionable)} service={service}",
            output_summary=(f"verdict={verdict} fresh_evidence={len(fresh_evidence)}"),
        )

        return {
            **state,
            "verify_result": verdict,
            "verify_evidence": fresh_evidence,
            "verify_gates": gate_results,
            "_verify_cycles": cycles + 1,
            "phase": "verified",
        }  # type: ignore[typeddict-unknown-key]

    except Exception as exc:
        logger.error(
            "verify: node failed incident=%s",
            state.get("incident_id"),
            exc_info=True,
        )
        deps.node_tracer(
            node_id=node_id,
            agent_run_id=state["agent_run_id"],
            name="verify",
            status="failed",
            started_at=started_at,
            finished_at=utc_now(),
            error_message=str(exc),
        )
        errors = list(state.get("errors", []))
        errors.append({"node": "verify", "error": str(exc)})
        return {
            **state,
            "verify_result": "error",
            "verify_evidence": [],
            "verify_gates": [],
            "phase": "verified",
            "errors": errors,
        }  # type: ignore[typeddict-unknown-key]


def _build_gate_plan(actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build a de-duplicated verify gate plan from action capability metadata.

    Multiple actions can share a gate. Required status is merged upward: if any
    action requires a gate, the combined gate remains required.
    """
    planned: list[dict[str, Any]] = []
    seen: dict[tuple[str, ...], int] = {}
    for action in actions:
        gates = _action_verify_gates(action)
        if not gates:
            gates = _DEFAULT_VERIFY_GATES
        for gate in gates:
            required = _gate_required(gate, action)
            key = _gate_key(gate, action)
            if key in seen:
                existing = planned[seen[key]]
                existing["required"] = bool(existing["required"]) or required
                continue
            seen[key] = len(planned)
            planned.append(
                {
                    "gate": gate,
                    "required": required,
                    "action_type": canonical_action_type(action.get("type")),
                    "target": action.get("target", ""),
                    "action_id": action.get("action_id", ""),
                }
            )
            expected_replicas = _expected_scale_replicas(action)
            if expected_replicas is not None:
                planned[-1]["expected_replicas"] = expected_replicas
    return planned


def _gate_key(gate: str, action: dict[str, Any]) -> tuple[str, ...]:
    """Return the de-duplication key for a gate/action pair."""
    action_type = canonical_action_type(action.get("type"))
    if gate == "k8s_rollout":
        expected_replicas = _expected_scale_replicas(action)
        if action_type in {"scale_deployment", "scale_back"} and expected_replicas is not None:
            return (
                gate,
                str(action.get("target", "")),
                action_type,
                str(expected_replicas),
            )
        return (
            gate,
            str(action.get("target", "")),
            action_type,
        )
    return gate, ""


def _expected_scale_replicas(action: dict[str, Any]) -> float | None:
    """Extract expected replica count for scale verification."""
    if canonical_action_type(action.get("type")) not in {"scale_deployment", "scale_back"}:
        return None

    params = action.get("params")
    if isinstance(params, dict) and "replicas" in params:
        return _number_value(params.get("replicas"))

    execution_result = action.get("execution_result")
    if isinstance(execution_result, dict):
        details = execution_result.get("details")
        if isinstance(details, dict):
            return _number_value(details.get("replicas"))
    return None


def _action_verify_gates(action: dict[str, Any]) -> tuple[str, ...]:
    """Prefer execution-attached gates, then fall back to static registry."""
    capability = action.get("capability")
    if isinstance(capability, dict):
        gates = capability.get("verify_gates", [])
        if isinstance(gates, list | tuple):
            return tuple(str(g) for g in gates if str(g))

    registered = get_action_capability(canonical_action_type(action.get("type")))
    if registered is not None:
        return registered.verify_gates
    return ()


def _gate_required(gate: str, action: dict[str, Any]) -> bool:
    """Return whether a gate failure/unknown should block resolved verdicts.

    Params may promote optional gates to required for stricter checks, but they
    never demote registry-required gates.
    """
    params = action.get("params", {})
    if isinstance(params, dict):
        required = params.get("required_verify_gates")
        if isinstance(required, list | tuple | set) and gate in required:
            return True
    return gate not in _OPTIONAL_VERIFY_GATES


def _run_verify_gate(
    gate: dict[str, Any],
    *,
    state: IncidentState,
    deps: AgentDeps,
    agent_run_id: str,
    service: str,
    alert_name: str,
    fresh_start: datetime,
    fresh_end: datetime,
) -> dict[str, Any]:
    """Dispatch one read-only verification gate."""
    gate_name = str(gate["gate"])
    required = bool(gate.get("required", True))
    if gate_name == "metrics_logs":
        return _run_metrics_logs_gate(
            gate,
            state=state,
            deps=deps,
            agent_run_id=agent_run_id,
            service=service,
            alert_name=alert_name,
            fresh_start=fresh_start,
            fresh_end=fresh_end,
        )
    if gate_name == "k8s_rollout":
        return _run_k8s_rollout_gate(gate, deps=deps, agent_run_id=agent_run_id, service=service)
    if gate_name == "db_readonly":
        return _run_db_readonly_gate(gate, state=state, deps=deps, agent_run_id=agent_run_id)
    return {
        **_gate_base(gate),
        "verdict": "degraded" if required else "unknown",
        "status": "failed",
        "summary": f"unknown verify gate '{gate_name}'",
        "_evidence": [],
    }


def _run_metrics_logs_gate(
    gate: dict[str, Any],
    *,
    state: IncidentState,
    deps: AgentDeps,
    agent_run_id: str,
    service: str,
    alert_name: str,
    fresh_start: datetime,
    fresh_end: datetime,
) -> dict[str, Any]:
    """Re-query metrics and logs and compare them to pre-action evidence."""
    metric_type = _metric_for_alert(alert_name)
    fresh_metrics = _safe_query_metrics(
        deps, agent_run_id, service, metric_type, fresh_start, fresh_end
    )
    fresh_logs = _safe_query_logs(deps, agent_run_id, service, alert_name, fresh_start, fresh_end)
    evidence = fresh_metrics + fresh_logs
    original = state.get("metrics_evidence", []) + state.get("logs_evidence", [])
    verdict = _assess_verification(original, evidence)
    status = "succeeded" if evidence else "degraded"
    return {
        **_gate_base(gate),
        "verdict": verdict,
        "status": status,
        "summary": f"metrics_logs verdict={verdict} evidence={len(evidence)}",
        "_evidence": evidence,
    }


def _run_k8s_rollout_gate(
    gate: dict[str, Any],
    *,
    deps: AgentDeps,
    agent_run_id: str,
    service: str,
) -> dict[str, Any]:
    """Verify live K8s state through the read-only diagnostics tool."""
    required = bool(gate.get("required", True))
    if deps.k8s_tool is None:
        verdict = "degraded" if required else "unknown"
        return {
            **_gate_base(gate),
            "verdict": verdict,
            "status": "degraded",
            "summary": "k8s diagnostics tool unavailable",
            "_evidence": [],
        }

    target = str(gate.get("target") or service)
    namespace = effective_executor_k8s_namespace(deps.settings)
    operation = (
        "get_statefulset"
        if str(gate.get("action_type", "")).lower() == "restart_statefulset"
        else "rollout_status"
    )
    action_type = str(gate.get("action_type", "")).lower()
    result = None
    verdict = "unknown"
    resolved_target = target
    last_error: Exception | None = None
    fallback_used = False

    for candidate_target in _rollout_target_candidates(target, service):
        query = K8sQuery(service=candidate_target, operation=operation, namespace=namespace)
        try:
            result = deps.k8s_tool.run(query)
            deps.tool_call_recorder(
                agent_run_id=agent_run_id,
                node_name="verify",
                tool_name=deps.k8s_tool.name,
                query=query,
                result=result,
                input_summary=(
                    "verify k8s_rollout "
                    f"service={candidate_target} namespace={namespace} operation={operation}"
                ),
            )
        except Exception as exc:
            last_error = exc
            logger.error(
                "verify: k8s rollout gate failed service=%s namespace=%s",
                candidate_target,
                namespace,
                exc_info=True,
            )
            continue

        verdict = _assess_k8s_rollout(
            result.data,
            result.status,
            action_type=action_type,
            expected_replicas=_number_value(gate.get("expected_replicas")),
            required=required,
        )
        resolved_target = candidate_target
        if (
            candidate_target != _normalize_rollout_target(service)
            and _should_try_rollout_service_fallback(result.status, verdict)
        ):
            fallback_used = True
            continue
        break

    if result is None:
        verdict = "degraded" if required else "unknown"
        summary = (
            f"k8s rollout gate unavailable: {type(last_error).__name__}"
            if last_error
            else "k8s rollout gate unavailable"
        )
        return {
            **_gate_base(gate),
            "verdict": verdict,
            "status": "degraded",
            "summary": summary,
            "_evidence": [],
        }

    evidence = [
        {
            **item,
            "_verify_fresh": True,
            "verify_gate": "k8s_rollout",
            "verify_target": resolved_target,
        }
        for item in result.evidence
    ]
    summary = result.summary
    if fallback_used and resolved_target != _normalize_rollout_target(target):
        summary = f"{summary}; fallback_target={resolved_target}"
    return {
        **_gate_base(gate),
        "verdict": verdict,
        "status": result.status,
        "summary": summary,
        "resolved_target": resolved_target,
        "_evidence": evidence,
    }


def _rollout_target_candidates(primary: str, service: str) -> list[str]:
    """Return verify targets, preferring the action target then incident service."""
    return _dedupe_nonempty(
        [
            _normalize_rollout_target(primary),
            _normalize_rollout_target(service),
        ]
    )


def _normalize_rollout_target(target: object) -> str:
    """Normalize common Kubernetes resource target strings for read-only verify."""
    value = str(target or "").strip()
    if "/" not in value:
        return value
    kind, name = value.split("/", 1)
    if kind.strip().lower() in {"deploy", "deployment", "statefulset", "sts"}:
        return name.strip()
    return value


def _dedupe_nonempty(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if not item or item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def _should_try_rollout_service_fallback(status: str, verdict: str) -> bool:
    """Return true when a rollout check likely failed due to a bad read target."""
    return status in {"degraded", "timeout", "failed"} or verdict == "unknown"


def _run_db_readonly_gate(
    gate: dict[str, Any],
    *,
    state: IncidentState,
    deps: AgentDeps,
    agent_run_id: str,
) -> dict[str, Any]:
    """Verify database pressure using only predefined read-only diagnostics."""
    required = bool(gate.get("required", True))
    if deps.db_diagnostics_tool is None:
        verdict = "degraded" if required else "unknown"
        return {
            **_gate_base(gate),
            "verdict": verdict,
            "status": "degraded",
            "summary": "db diagnostics tool unavailable",
            "_evidence": [],
        }

    query = DbDiagnosticsQuery(operation="connection_pool")
    try:
        result = deps.db_diagnostics_tool.run(query)
        deps.tool_call_recorder(
            agent_run_id=agent_run_id,
            node_name="verify",
            tool_name=deps.db_diagnostics_tool.name,
            query=query,
            result=result,
            input_summary="verify db_readonly op=connection_pool",
        )
    except Exception as exc:
        logger.error("verify: db readonly gate failed", exc_info=True)
        verdict = "degraded" if required else "unknown"
        return {
            **_gate_base(gate),
            "verdict": verdict,
            "status": "degraded",
            "summary": f"db readonly gate unavailable: {type(exc).__name__}",
            "_evidence": [],
        }
    evidence = [
        {**item, "_verify_fresh": True, "verify_gate": "db_readonly"} for item in result.evidence
    ]
    verdict = _assess_db_readonly(
        state.get("db_evidence", []),
        result.data,
        result.status,
        required=required,
    )
    return {
        **_gate_base(gate),
        "verdict": verdict,
        "status": result.status,
        "summary": result.summary,
        "_evidence": evidence,
    }


def _gate_base(gate: dict[str, Any]) -> dict[str, Any]:
    """Create the common public shape for all gate verdicts."""
    base = {
        "gate": str(gate.get("gate", "")),
        "required": bool(gate.get("required", True)),
        "action_type": str(gate.get("action_type", "")),
        "target": str(gate.get("target", "")),
        "action_id": str(gate.get("action_id", "")),
    }
    if "expected_replicas" in gate:
        base["expected_replicas"] = gate["expected_replicas"]
    return base


def _safe_query_metrics(
    deps: AgentDeps,
    agent_run_id: str,
    service: str,
    metric_type: MetricType,
    start: datetime,
    end: datetime,
) -> list[dict[str, Any]]:
    """Best-effort metrics query for verification.

    Tool failures degrade the gate through missing evidence rather than raising
    out of the node, keeping the overall incident report available.
    """
    try:
        query = MetricsQuery(service=service, metric_type=metric_type, start=start, end=end)
        result = deps.metrics_tool.run(query)
        deps.tool_call_recorder(
            agent_run_id=agent_run_id,
            node_name="verify",
            tool_name=deps.metrics_tool.name,
            query=query,
            result=result,
            input_summary=f"verify metric={metric_type} service={service}",
        )
        if result.evidence:
            return [{**e, "_verify_fresh": True} for e in result.evidence]
        return [
            {
                "type": "metric",
                "source": "prometheus",
                "metric_type": metric_type,
                "service": service,
                "status": result.status,
                "summary": result.summary,
                "_verify_fresh": True,
            }
        ]
    except Exception:
        logger.error(
            "verify: metrics_tool failed service=%s metric=%s",
            service,
            metric_type,
            exc_info=True,
        )
        return []


def _safe_query_logs(
    deps: AgentDeps,
    agent_run_id: str,
    service: str,
    alert_name: str,
    start: datetime,
    end: datetime,
) -> list[dict[str, Any]]:
    """Best-effort log query for verification."""
    try:
        keywords = _keywords_for_alert(alert_name)
        query = LogsQuery(service=service, start=start, end=end, keywords=keywords, limit=50)
        result = deps.logs_tool.run(query)
        deps.tool_call_recorder(
            agent_run_id=agent_run_id,
            node_name="verify",
            tool_name=deps.logs_tool.name,
            query=query,
            result=result,
            input_summary=f"verify logs service={service}",
        )
        if result.evidence:
            return [{**e, "_verify_fresh": True} for e in result.evidence]
        return [
            {
                "type": "log",
                "source": "loki",
                "service": service,
                "status": result.status,
                "summary": result.summary,
                "_verify_fresh": True,
            }
        ]
    except Exception:
        logger.error(
            "verify: logs_tool failed service=%s",
            service,
            exc_info=True,
        )
        return []


def _assess_verification(
    original: list[dict[str, Any]],
    fresh: list[dict[str, Any]],
) -> str:
    """Compare pre-execution and post-execution evidence deterministically.

    Returns one of:
    - ``"resolved"`` — error rate returned to baseline or no errors in fresh data
    - ``"improving"`` — meaningful improvement but not fully resolved
    - ``"unchanged"`` — no meaningful change detected
    - ``"degraded"`` — situation worsened (error rate doubled, new errors appeared)
    - ``"unknown"`` — insufficient data to compare
    """
    if not fresh:
        return "unknown"

    # Extract numeric values from evidence summaries.
    # Relies on ``compact_summary`` output format ("key=value, key2=value2").
    def _extract_value(evidence_list: list[dict[str, Any]], key: str) -> float | None:
        for item in evidence_list:
            summary = str(item.get("summary", ""))
            for part in summary.replace(",", " ").split():
                if "=" in part:
                    k, v = part.split("=", 1)
                    if k.strip() == key:
                        try:
                            return float(v.strip())
                        except (ValueError, TypeError):
                            pass
            data = item.get("data", {})
            if isinstance(data, dict):
                val = data.get(key)
                if val is not None:
                    try:
                        return float(val)
                    except (ValueError, TypeError):
                        pass
        return None

    orig_error = _extract_value(original, "error_rate")
    fresh_error = _extract_value(fresh, "error_rate")
    orig_latency = _extract_value(original, "latency_ms")
    fresh_latency = _extract_value(fresh, "latency_ms")

    # Fallback: compare evidence counts with error-like content.
    fresh_failures = sum(
        1
        for e in fresh
        if "error" in str(e.get("summary", "")).lower() or e.get("status") == "failed"
    )
    orig_failures = sum(
        1
        for e in original
        if "error" in str(e.get("summary", "")).lower() or e.get("status") == "failed"
    )

    improved = False

    # Error rate comparison (most reliable signal).
    # Degraded: error rate more than doubled (guard: orig_error > 0).
    if fresh_error is not None and orig_error is not None and orig_error > 0:
        if fresh_error / orig_error > 2.0:
            return "degraded"

    if fresh_error is not None:
        if fresh_error < _ERROR_RATE_RESOLVED:
            return "resolved"
        if orig_error is not None and orig_error > 0:
            drop = (orig_error - fresh_error) / orig_error
            if drop > _ERROR_RATE_IMPROVED:
                improved = True
    elif orig_error is not None and fresh_error is None:
        # Errors disappeared — improvement.
        improved = True

    # Latency comparison.
    if fresh_latency is not None:
        if fresh_latency < _LATENCY_RESOLVED_MS:
            return "resolved"
        if orig_latency is not None and orig_latency > 0:
            drop = (orig_latency - fresh_latency) / orig_latency
            if drop > _LATENCY_IMPROVED_MS:
                improved = True

    # Degraded via substantial increase with corroborating failure counts.
    # (separate from the 2x check above; this catches moderate but
    # concerning increases when there were zero failures before).
    if fresh_error is not None and orig_error is not None:
        if fresh_error > orig_error * 1.5:
            if fresh_failures > orig_failures + 2:
                return "degraded"
    if orig_failures == 0 and fresh_failures > 2:
        return "degraded"

    # Count-based fallback.
    if fresh_failures < orig_failures:
        improved = True
    if orig_failures > 0 and fresh_failures == 0:
        return "resolved"

    if improved:
        return "improving"
    return "unchanged"


def _assess_k8s_rollout(
    data: dict[str, Any],
    status: str,
    *,
    action_type: str = "",
    expected_replicas: float | None = None,
    required: bool,
) -> str:
    """Assess a read-only rollout status payload.

    Required gates fail closed on unavailable/degraded data. Optional gates may
    return unknown so an unrelated optional signal does not block resolution.
    """
    if status in {"failed", "timeout"}:
        return "degraded" if required else "unknown"
    if status == "degraded":
        return "unknown" if not required else "degraded"

    payload = data.get("payload", data)
    if not isinstance(payload, dict) or not payload:
        return "degraded" if required else "unknown"
    if payload.get("error"):
        return "degraded" if required else "unknown"

    rollout_status = str(payload.get("status", "")).strip().lower()
    desired = _first_number(payload, "desired_replicas", "replicas")
    if action_type in {"scale_deployment", "scale_back"} and expected_replicas is not None:
        if desired is None:
            return "unknown"
        if desired != expected_replicas:
            return "unchanged"

    if action_type == "pause_rollout":
        if payload.get("paused") is True or rollout_status == "paused":
            return "resolved"
        if rollout_status in {"failed", "failure", "degraded"}:
            return "degraded"
        return "unchanged"
    if action_type == "resume_rollout" and (
        payload.get("paused") is True or rollout_status == "paused"
    ):
        return "unchanged"

    if rollout_status in {"failed", "failure", "degraded"}:
        return "degraded"
    if rollout_status in {"complete", "completed", "successful", "success"}:
        return "resolved"
    if rollout_status in {"progressing", "in_progress"}:
        return "improving"
    if rollout_status in {"pending", "paused"}:
        return "unchanged"
    if action_type in {"scale_deployment", "scale_back"} and expected_replicas == 0:
        if desired == 0:
            return "resolved"

    for condition in payload.get("conditions", []) or []:
        if not isinstance(condition, dict):
            continue
        ctype = str(condition.get("type", ""))
        cstatus = str(condition.get("status", "")).lower()
        if ctype == "ReplicaFailure" and cstatus == "true":
            return "degraded"
        if ctype == "Progressing" and cstatus == "false":
            return "degraded"

    ready = _first_number(payload, "ready_replicas", "available_replicas")
    updated = _first_number(payload, "updated_replicas")
    if desired is not None and desired > 0:
        if ready is not None and updated is not None:
            if ready >= desired and updated >= desired:
                return "resolved"
            if ready > 0 or updated > 0:
                return "improving"
            return "unchanged"
        if ready is not None:
            if ready >= desired:
                return "resolved"
            if ready > 0:
                return "improving"
            return "unchanged"

    return "unknown"


def _assess_db_readonly(
    original: list[dict[str, Any]],
    data: dict[str, Any],
    status: str,
    *,
    required: bool,
) -> str:
    """Assess read-only connection-pool diagnostics."""
    if status in {"failed", "timeout"}:
        return "degraded" if required else "unknown"
    if status == "degraded":
        return "degraded" if required else "unknown"

    rows = data.get("rows", [])
    if not isinstance(rows, list) or not rows:
        return "degraded" if required else "unknown"

    fresh_connections = _connection_count_from_rows(rows)
    if fresh_connections is None:
        return "unknown"
    if fresh_connections <= _DB_CONNECTION_RESOLVED:
        return "resolved"

    original_connections = _connection_count_from_evidence(original)
    if original_connections is None:
        return "unknown"
    if original_connections > 0 and fresh_connections > original_connections * 1.5:
        return "degraded"
    if original_connections > 0:
        drop = (original_connections - fresh_connections) / original_connections
        if drop >= _DB_CONNECTION_IMPROVED:
            return "improving"
    return "unchanged"


def _combine_gate_verdicts(gates: list[dict[str, Any]]) -> str:
    """Collapse gate verdicts into the workflow-level verify result.

    Degraded dominates, unknown required gates keep the result unknown, and any
    unchanged/improving gate prevents the workflow from declaring full success.
    """
    if not gates:
        return "unknown"

    effective = [gate for gate in gates if gate.get("required") or gate.get("verdict") != "unknown"]
    if not effective:
        return "unknown"

    verdicts = [str(gate.get("verdict", "unknown")) for gate in effective]
    if "degraded" in verdicts:
        return "degraded"
    required_verdicts = [
        str(gate.get("verdict", "unknown")) for gate in gates if gate.get("required")
    ]
    if "unknown" in required_verdicts:
        return "unknown"
    if "unchanged" in verdicts:
        return "unchanged"
    if "improving" in verdicts:
        return "improving"
    if verdicts and all(verdict == "resolved" for verdict in verdicts):
        return "resolved"
    return "unknown"


def _connection_count_from_rows(rows: list[Any]) -> float | None:
    total = 0.0
    saw_count = False
    for row in rows:
        if not isinstance(row, dict):
            continue
        value = row.get("connections")
        if value is None:
            continue
        try:
            total += float(value)
            saw_count = True
        except (TypeError, ValueError):
            continue
    return total if saw_count else None


def _connection_count_from_evidence(evidence: list[dict[str, Any]]) -> float | None:
    for item in evidence:
        rows = _rows_from_evidence_payload(item)
        if rows:
            count = _connection_count_from_rows(rows)
            if count is not None:
                return count
        summary_count = _summary_number(item, "connections")
        if summary_count is not None:
            return summary_count
    return None


def _rows_from_evidence_payload(item: dict[str, Any]) -> list[Any]:
    payload = item.get("payload")
    if isinstance(payload, dict):
        rows = payload.get("rows")
        if isinstance(rows, list):
            return rows
        data = payload.get("data")
        if isinstance(data, dict):
            nested_rows = data.get("rows")
            if isinstance(nested_rows, list):
                return nested_rows
    data = item.get("data")
    if isinstance(data, dict):
        rows = data.get("rows")
        if isinstance(rows, list):
            return rows
    return []


def _summary_number(item: dict[str, Any], key: str) -> float | None:
    summary = str(item.get("summary", ""))
    for part in summary.replace(",", " ").split():
        if "=" not in part:
            continue
        k, value = part.split("=", 1)
        if k.strip() != key:
            continue
        try:
            return float(value.strip())
        except (TypeError, ValueError):
            return None
    return None


def _first_number(payload: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = payload.get(key)
        if value is None:
            continue
        number = _number_value(value)
        if number is not None:
            return number
    return None


def _number_value(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _metric_for_alert(alert_name: str) -> MetricType:
    """Reuse the same mapping as collect_metrics for consistency."""
    n = alert_name.lower()
    if "db" in n or "connection" in n:
        return "db_connections"
    if "cache" in n or "redis" in n:
        return "cache_hit_rate"
    if "throttl" in n or "cpu" in n:
        return "cpu_throttle"
    if "leak" in n or "oom" in n:
        return "memory"
    if "pod" in n or "restart" in n:
        return "memory"
    if "disk" in n:
        return "disk_avail"
    if "cert" in n:
        return "cert_expiry_days"
    if "dns" in n:
        return "dns_error_rate"
    if "queue" in n or "lag" in n or "kafka" in n:
        return "queue_lag"
    if "ratelimit" in n or "rate_limit" in n:
        return "rate_limit_hits"
    if "budget" in n or "burn" in n:
        return "slo_burn_rate"
    if "slow" in n or "latency" in n or "timeout" in n:
        return "latency"
    return "error_rate"


def _keywords_for_alert(alert_name: str) -> list[str]:
    """Reuse the same mapping as collect_logs for consistency."""
    n = alert_name.lower()
    if "db" in n or "connection" in n:
        return ["database", "connection", "exhausted"]
    if "cache" in n or "redis" in n:
        return ["redis", "cache", "miss"]
    if "pod" in n or "restart" in n:
        return ["restart", "oom", "kubernetes"]
    return ["5xx", "error", "deploy"]
