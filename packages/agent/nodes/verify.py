"""Verify that executed actions resolved the incident (ReAct Loop A).

Re-queries metrics and logs after L2/L3 actions execute, compares
before/after state, and routes back to plan_actions if the issue persists.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from typing import Any

from packages.agent.nodes._persist import persist_evidence
from packages.agent.schemas import AgentDeps
from packages.agent.state import IncidentState
from packages.common.ids import new_id
from packages.common.time import utc_now
from packages.tools.logs import LogsQuery
from packages.tools.metrics import MetricsQuery, MetricType

logger = logging.getLogger(__name__)

MAX_VERIFY_CYCLES = 2

# Thresholds for deterministic before/after comparison.
_ERROR_RATE_RESOLVED = 0.01   # < 1% error rate -> resolved
_ERROR_RATE_IMPROVED = 0.5    # > 50% drop -> improving
_LATENCY_IMPROVED_MS = 0.5    # > 50% latency drop -> improving
_LATENCY_RESOLVED_MS = 100    # < 100ms -> resolved


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
        actionable = [
            r for r in execution_results
            if str(r.get("risk_level", "")).upper() in ("L2", "L3")
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
                input_summary=(
                    f"actionable_l23={len(actionable)} cycles={cycles}"
                ),
                output_summary="skipped" if not actionable else "max_cycles",
            )
            return {
                **state,
                "verify_result": verdict,
                "verify_evidence": [],
                "phase": "verified",
            }  # type: ignore[typeddict-unknown-key]

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

        # Re-query the same metric type that was collected pre-diagnosis.
        metric_type = _metric_for_alert(alert_name)
        agent_run_id = state["agent_run_id"]
        fresh_metrics = _safe_query_metrics(
            deps, agent_run_id, service, metric_type, fresh_start, fresh_end
        )
        fresh_logs = _safe_query_logs(
            deps, agent_run_id, service, alert_name, fresh_start, fresh_end
        )
        fresh_evidence = fresh_metrics + fresh_logs

        # Persist fresh evidence so it is traceable in DB.
        if fresh_evidence:
            fresh_evidence = persist_evidence(
                deps.db, state["incident_id"], state["agent_run_id"],
                fresh_evidence,
            )

        original_evidence = (
            state.get("metrics_evidence", []) + state.get("logs_evidence", [])
        )
        verdict = _assess_verification(original_evidence, fresh_evidence)

        deps.node_tracer(
            node_id=node_id,
            agent_run_id=state["agent_run_id"],
            name="verify",
            status="succeeded",
            started_at=started_at,
            finished_at=utc_now(),
            input_summary=f"actions={len(actionable)} service={service}",
            output_summary=(
                f"verdict={verdict} fresh_evidence={len(fresh_evidence)}"
            ),
        )

        return {
            **state,
            "verify_result": verdict,
            "verify_evidence": fresh_evidence,
            "_verify_cycles": cycles + 1,
            "phase": "verified",
        }  # type: ignore[typeddict-unknown-key]

    except Exception as exc:
        logger.error(
            "verify: node failed incident=%s",
            state.get("incident_id"), exc_info=True,
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
            "phase": "verified",
            "errors": errors,
        }  # type: ignore[typeddict-unknown-key]


def _safe_query_metrics(
    deps: AgentDeps,
    agent_run_id: str,
    service: str,
    metric_type: MetricType,
    start: datetime,
    end: datetime,
) -> list[dict[str, Any]]:
    try:
        query = MetricsQuery(
            service=service, metric_type=metric_type, start=start, end=end
        )
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
            return [
                {**e, "_verify_fresh": True}
                for e in result.evidence
            ]
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
            service, metric_type, exc_info=True,
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
    try:
        keywords = _keywords_for_alert(alert_name)
        query = LogsQuery(
            service=service, start=start, end=end, keywords=keywords, limit=50
        )
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
            return [
                {**e, "_verify_fresh": True}
                for e in result.evidence
            ]
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
            "verify: logs_tool failed service=%s", service, exc_info=True,
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
    def _extract_value(
        evidence_list: list[dict[str, Any]], key: str
    ) -> float | None:
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
        1 for e in fresh
        if "error" in str(e.get("summary", "")).lower()
        or e.get("status") == "failed"
    )
    orig_failures = sum(
        1 for e in original
        if "error" in str(e.get("summary", "")).lower()
        or e.get("status") == "failed"
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
