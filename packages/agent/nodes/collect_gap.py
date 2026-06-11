"""Targeted evidence re-collection based on diagnosis gaps (ReAct Loop B).

When the LLM diagnosis identifies missing evidence, this node parses the
free-text gap descriptions, matches them to available tools via keyword
heuristics, re-queries those tools with an expanded time window, and
appends the fresh evidence to the existing lists for a second diagnosis
pass.
"""

from __future__ import annotations

import logging
import re
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

MAX_DIAGNOSE_CYCLES = 1

# Keyword -> (tool_name, query_builder) mapping.
# Each entry: a list of regex patterns; when a missing_evidence string matches
# any pattern, the corresponding tool is queried.
_TOOL_MATCHERS: list[tuple[str, list[str]]] = [
    ("metrics", [
        r"metric", r"cpu", r"memory", r"latency", r"error.rate", r"qps",
        r"throughput", r"throttl", r"disk", r"cert.expir", r"dns",
        r"queue.lag", r"rate.limit", r"slo", r"burn.rate",
    ]),
    ("logs", [
        r"\blog", r"error\b", r"exception", r"stack", r"traceback",
        r"warn", r"crash", r"panic",
    ]),
    ("traces", [
        r"trace", r"span", r"distributed", r"call.chain", r"propagation",
    ]),
    ("deployment", [
        r"\bdeploy", r"release", r"commit", r"push", r"rollout",
        r"git.change", r"revert",
    ]),
    ("k8s", [
        r"pod", r"container", r"node", r"cluster", r"namespace",
        r"deployment\b", r"statefulset", r"daemonset",
    ]),
    ("db", [
        r"\bdb\b", r"connection.pool", r"slow.query", r"table.lock",
        r"database", r"postgres", r"mysql", r"query.plan",
    ]),
]

# Expand the time window by this many minutes on each side for gap queries.
_WINDOW_EXPAND_MINUTES = 5


def collect_gap(state: IncidentState, deps: AgentDeps) -> IncidentState:
    """Re-query tools based on missing_evidence from the LLM diagnosis.

    Appends fresh evidence to the existing evidence lists without overwriting
    so the second diagnosis pass sees both the original and the gap-fill data.
    """
    node_id = new_id("nd_")
    started_at = utc_now()
    try:
        cycles = int(state.get("_collect_gap_cycles", 0))
        if cycles >= MAX_DIAGNOSE_CYCLES:
            deps.node_tracer(
                node_id=node_id,
                agent_run_id=state["agent_run_id"],
                name="collect_gap",
                status="succeeded",
                started_at=started_at,
                finished_at=utc_now(),
                input_summary=f"cycles={cycles}",
                output_summary="max_cycles",
            )
            return {**state, "phase": "gap_collection_skipped"}  # type: ignore[typeddict-unknown-key]

        # Extract missing_evidence from the diagnosis rationale.
        rationale = state.get("diagnosis_rationale", {})
        missing = list(rationale.get("missing_evidence", []))
        if not missing:
            deps.node_tracer(
                node_id=node_id,
                agent_run_id=state["agent_run_id"],
                name="collect_gap",
                status="succeeded",
                started_at=started_at,
                finished_at=utc_now(),
                input_summary="no gaps",
                output_summary="skipped",
            )
            return {**state, "phase": "gap_collection_skipped"}  # type: ignore[typeddict-unknown-key]

        # Determine which tools to query.
        tools_to_query = _match_tools(missing)

        # Build an expanded time window with safe parsing.
        tw = state.get("time_window", {})
        original_start = _parse_time(tw.get("start"), utc_now() - timedelta(minutes=15))
        original_end = _parse_time(tw.get("end"), utc_now())
        gap_start = original_start - timedelta(minutes=_WINDOW_EXPAND_MINUTES)
        gap_end = original_end + timedelta(minutes=_WINDOW_EXPAND_MINUTES)

        service = state.get("service_name", "unknown")
        alert_name = state.get("alert_name", "UnknownAlert")

        # Execute gap queries. Each tool runs independently; failures in one
        # do not block the others.
        collected: dict[str, list[dict[str, Any]]] = {}
        tools_failed = 0

        if "metrics" in tools_to_query:
            metric_type = _metric_for_alert(alert_name)
            collected["metrics"] = _safe_query_metrics(
                deps, service, metric_type, gap_start, gap_end
            )
            if not collected["metrics"]:
                tools_failed += 1

        if "logs" in tools_to_query:
            collected["logs"] = _safe_query_logs(
                deps, service, alert_name, gap_start, gap_end
            )
            if not collected["logs"]:
                tools_failed += 1

        if "traces" in tools_to_query:
            collected["traces"] = _safe_query_traces(deps, service, gap_start, gap_end)
            if not collected["traces"]:
                tools_failed += 1

        if "deployment" in tools_to_query:
            collected["deployment"] = _safe_query_deployment(
                deps, service, gap_start, gap_end
            )
            if not collected["deployment"]:
                tools_failed += 1

        if "k8s" in tools_to_query:
            collected["k8s"] = _safe_query_k8s(deps, service)
            if not collected["k8s"]:
                tools_failed += 1

        if "db" in tools_to_query:
            collected["db"] = _safe_query_db(deps, service)
            if not collected["db"]:
                tools_failed += 1

        # Persist and merge fresh evidence into existing lists.
        evidence_keys: dict[str, str] = {
            "metrics": "metrics_evidence",
            "logs": "logs_evidence",
            "traces": "traces_evidence",
            "deployment": "deployment_evidence",
            "k8s": "k8s_evidence",
            "db": "db_evidence",
        }
        state_update: dict[str, Any] = {
            "_collect_gap_cycles": cycles + 1,
            "phase": "gap_collected",
        }

        for tool_name, evidence_list in collected.items():
            if not evidence_list:
                continue
            # Use immutable pattern (copy each item) to avoid mutating
            # cached ToolResult evidence dicts.
            tagged = [
                {**item, "_collected_in_gap": True} for item in evidence_list
            ]
            persisted = persist_evidence(
                deps.db, state["incident_id"], state["agent_run_id"], tagged
            )
            state_key = evidence_keys[tool_name]
            existing = list(state.get(state_key, []) or [])  # type: ignore[call-overload]
            state_update[state_key] = existing + persisted

        tool_names = sorted(collected.keys())
        tools_with_data = sum(
            1 for v in collected.values() if v
        )
        deps.node_tracer(
            node_id=node_id,
            agent_run_id=state["agent_run_id"],
            name="collect_gap",
            status="succeeded" if tools_failed == 0 else "degraded",
            started_at=started_at,
            finished_at=utc_now(),
            input_summary=f"missing={len(missing)} tools={tool_names}",
            output_summary=(
                f"collected_from={tools_with_data}/{len(tool_names)} "
                f"tools_failed={tools_failed}"
            ),
        )

        return {**state, **state_update}  # type: ignore[typeddict-item,typeddict-unknown-key]

    except Exception as exc:
        logger.error(
            "collect_gap: node failed incident=%s",
            state.get("incident_id"), exc_info=True,
        )
        deps.node_tracer(
            node_id=node_id,
            agent_run_id=state["agent_run_id"],
            name="collect_gap",
            status="failed",
            started_at=started_at,
            finished_at=utc_now(),
            error_message=str(exc),
        )
        errors = list(state.get("errors", []))
        errors.append({"node": "collect_gap", "error": str(exc)})
        # Increment cycle counter on error to prevent infinite loop
        # when a tool backend consistently fails.
        return {**state, "errors": errors, "_collect_gap_cycles": cycles + 1}


def _match_tools(missing: list[str]) -> set[str]:
    """Map missing_evidence strings to tool names via keyword matching."""
    tools: set[str] = set()
    for item in missing:
        item_lower = item.lower()
        for tool_name, patterns in _TOOL_MATCHERS:
            for pattern in patterns:
                if re.search(pattern, item_lower):
                    tools.add(tool_name)
                    break
    return tools


def _parse_time(raw: object, default: datetime) -> datetime:
    """Safely parse an ISO-8601 timestamp, falling back to *default*."""
    if not raw:
        return default
    try:
        return datetime.fromisoformat(str(raw))
    except (ValueError, TypeError):
        logger.warning("collect_gap: unparseable time_window value: %s", raw)
        return default


# ---------------------------------------------------------------------------
# Safe query helpers — each catches exceptions and returns [] on failure.
# ---------------------------------------------------------------------------


def _safe_query_metrics(
    deps: AgentDeps,
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
        if result.evidence:
            return list(result.evidence)
        return [
            {
                "type": "metric", "source": "prometheus",
                "metric_type": metric_type, "service": service,
                "status": result.status, "summary": result.summary,
            }
        ]
    except Exception:
        logger.error(
            "collect_gap: metrics_tool failed service=%s metric=%s",
            service, metric_type, exc_info=True,
        )
        return []


def _safe_query_logs(
    deps: AgentDeps,
    service: str,
    alert_name: str,
    start: datetime,
    end: datetime,
) -> list[dict[str, Any]]:
    try:
        keywords = _keywords_for_alert(alert_name)
        query = LogsQuery(
            service=service, start=start, end=end, keywords=keywords, limit=100
        )
        result = deps.logs_tool.run(query)
        if result.evidence:
            return list(result.evidence)
        return [
            {
                "type": "log", "source": "loki", "service": service,
                "status": result.status, "summary": result.summary,
            }
        ]
    except Exception:
        logger.error(
            "collect_gap: logs_tool failed service=%s", service, exc_info=True,
        )
        return []


def _safe_query_traces(
    deps: AgentDeps, service: str, start: datetime, end: datetime
) -> list[dict[str, Any]]:
    try:
        from packages.tools.traces import TraceQuery
        query = TraceQuery(
            service=service, start=start, end=end, min_duration_ms=100
        )
        result = deps.trace_tool.run(query)
        if result.evidence:
            return list(result.evidence)
        return [
            {
                "type": "trace", "source": "tempo", "service": service,
                "status": result.status, "summary": result.summary,
            }
        ]
    except Exception:
        logger.error(
            "collect_gap: trace_tool failed service=%s", service, exc_info=True,
        )
        return []


def _safe_query_deployment(
    deps: AgentDeps, service: str, start: datetime, end: datetime
) -> list[dict[str, Any]]:
    try:
        from packages.tools.git_changes import GitChangeQuery
        query = GitChangeQuery(
            service=service, start=start, end=end
        )
        result = deps.git_change_tool.run(query)
        if result.evidence:
            return list(result.evidence)
        return [
            {
                "type": "deployment", "source": "git", "service": service,
                "status": result.status, "summary": result.summary,
            }
        ]
    except Exception:
        logger.error(
            "collect_gap: git_change_tool failed service=%s",
            service, exc_info=True,
        )
        return []


def _safe_query_k8s(deps: AgentDeps, service: str) -> list[dict[str, Any]]:
    if deps.k8s_tool is None:
        return []
    try:
        from packages.tools.k8s import K8sQuery
        query = K8sQuery(service=service)
        result = deps.k8s_tool.run(query)
        if result.evidence:
            return list(result.evidence)
        return [
            {
                "type": "k8s", "source": "kubernetes", "service": service,
                "status": result.status, "summary": result.summary,
            }
        ]
    except Exception:
        logger.error(
            "collect_gap: k8s_tool failed service=%s", service, exc_info=True,
        )
        return []


def _safe_query_db(deps: AgentDeps, service: str) -> list[dict[str, Any]]:
    if deps.db_diagnostics_tool is None:
        return []
    try:
        from packages.tools.db_diagnostics import DbDiagnosticsQuery
        query = DbDiagnosticsQuery(operation="connection_pool")
        result = deps.db_diagnostics_tool.run(query)
        if result.evidence:
            return list(result.evidence)
        return [
            {
                "type": "db", "source": "database", "service": service,
                "status": result.status, "summary": result.summary,
            }
        ]
    except Exception:
        logger.error(
            "collect_gap: db_diagnostics_tool failed service=%s",
            service, exc_info=True,
        )
        return []


# ---------------------------------------------------------------------------
# Alert-name -> type/keyword mappings (same heuristics as collect_metrics/logs).
# ---------------------------------------------------------------------------


def _metric_for_alert(alert_name: str) -> MetricType:
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
    n = alert_name.lower()
    if "db" in n or "connection" in n:
        return ["database", "connection", "exhausted"]
    if "cache" in n or "redis" in n:
        return ["redis", "cache", "miss"]
    if "pod" in n or "restart" in n:
        return ["restart", "oom", "kubernetes"]
    return ["5xx", "error", "deploy"]
