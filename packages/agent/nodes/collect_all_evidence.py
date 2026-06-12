"""Parallel evidence collection orchestrator.

Replaces the sequential chain of six individual ``collect_*`` LangGraph nodes
with a single node that fans out to all collectors concurrently via
:class:`~concurrent.futures.ThreadPoolExecutor`, merges the disjoint results,
replays captured tracing metadata on the main thread, and bulk-persists
evidence in a single DB transaction.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import copy
from typing import Any, NamedTuple

from packages.agent.nodes._persist import persist_evidence_batch
from packages.agent.nodes.collect_db import collect_db
from packages.agent.nodes.collect_deployment import collect_deployment
from packages.agent.nodes.collect_k8s import collect_k8s
from packages.agent.nodes.collect_logs import collect_logs
from packages.agent.nodes.collect_metrics import collect_metrics
from packages.agent.nodes.collect_traces import collect_traces
from packages.agent.schemas import AgentDeps
from packages.agent.state import IncidentState
from packages.common.ids import new_id
from packages.common.time import utc_now

_COLLECTORS: list[tuple[str, Any]] = [
    ("metrics", collect_metrics),
    ("logs", collect_logs),
    ("traces", collect_traces),
    ("deployment", collect_deployment),
    ("k8s", collect_k8s),
    ("db", collect_db),
]


class _CollectorResult(NamedTuple):
    """Per-thread result from a single evidence collector."""

    partial_state: dict[str, Any]
    node_trace_args: dict[str, Any] | None
    tool_call_args: list[dict[str, Any]]


def collect_all_evidence(state: IncidentState, deps: AgentDeps) -> IncidentState:
    """Run all evidence collectors in parallel and merge results.

    Three-phase execution (all DB writes happen on the main thread):

    1. **Parallel queries** — each collector runs in its own thread with
       capturing wrappers for ``node_tracer`` / ``tool_call_recorder`` so
       the shared ``deps.db`` session is never touched concurrently.
    2. **Merge** — partial state dicts are combined on the main thread.
    3. **Replay + persist** — captured trace calls are replayed on the
       real callbacks, then all evidence is bulk-persisted.
    """
    node_id = new_id("nd_")
    started_at = utc_now()

    # ---- Phase 1: parallel queries ----------------------------------------
    results: dict[str, _CollectorResult] = {}
    # Per-collector tool timeout with generous safety margin
    deadline = deps.settings.tool_timeout_seconds * len(_COLLECTORS) * 3
    with ThreadPoolExecutor(max_workers=len(_COLLECTORS)) as executor:
        futures = {
            executor.submit(_run_one, name, fn, state, deps): name
            for name, fn in _COLLECTORS
        }
        for future in as_completed(futures, timeout=deadline):
            name = futures[future]
            try:
                results[name] = future.result(timeout=deadline)
            except Exception as exc:
                results[name] = _CollectorResult(
                    partial_state={
                        "errors": [{"node": f"collect_{name}", "error": str(exc)}],
                    },
                    node_trace_args=None,
                    tool_call_args=[],
                )

    # ---- Phase 2: merge partial states ------------------------------------
    merged: dict[str, Any] = dict(state)
    all_errors: list[dict[str, Any]] = list(merged.get("errors", []))
    for r in results.values():
        partial = dict(r.partial_state)
        # Accumulate errors from each partial state before merging
        partial_errors = partial.pop("errors", None)
        if partial_errors:
            all_errors.extend(partial_errors)
        merged.update(partial)
    merged["errors"] = all_errors

    # ---- Phase 3: replay traces + batch persist ---------------------------
    for _name, r in results.items():
        try:
            if r.node_trace_args:
                deps.node_tracer(**r.node_trace_args)
            for tc_args in r.tool_call_args:
                deps.tool_call_recorder(**tc_args)
        except Exception:
            pass  # trace replay is best-effort; evidence persistence is critical

    evidence_by_source = {
        name: (merged.get(f"{name}_evidence") or []) for name, _ in _COLLECTORS
    }
    try:
        persist_evidence_batch(
            deps.db, state["incident_id"], state["agent_run_id"], evidence_by_source
        )
    except Exception as exc:
        # Clear any evidence_ids that may have been set before the failure
        # so downstream nodes don't reference non-existent DB rows.
        for _source_items in evidence_by_source.values():
            for _item in _source_items:
                _item.pop("evidence_id", None)
        all_errors.append({"node": "persist_evidence_batch", "error": str(exc)})
        merged["errors"] = all_errors

    merged["phase"] = "evidence_collected"

    deps.node_tracer(
        node_id=node_id,
        agent_run_id=state["agent_run_id"],
        name="collect_all_evidence",
        status="succeeded",
        started_at=started_at,
        finished_at=utc_now(),
        input_summary=f"service={state.get('service_name', 'unknown')}",
        output_summary=f"sources={','.join(results.keys())}",
    )

    return merged  # type: ignore[return-value]


def _run_one(
    name: str,
    fn: Any,
    state: IncidentState,
    deps: AgentDeps,
) -> _CollectorResult:
    """Execute a single collector with thread-safe trace capture.

    Replaces ``deps.node_tracer`` and ``deps.tool_call_recorder`` with
    capturing wrappers so the DB session is never touched from a worker
    thread.  The captured arguments are replayed on the main thread after
    all collectors complete.
    """
    captured_node: dict[str, Any] | None = None
    captured_tools: list[dict[str, Any]] = []
    lock = threading.Lock()

    def _capture_node(**kwargs: Any) -> None:
        nonlocal captured_node
        with lock:
            captured_node = dict(kwargs)

    def _capture_tool(**kwargs: Any) -> None:
        with lock:
            captured_tools.append(dict(kwargs))

    thread_deps = copy(deps)
    thread_deps.node_tracer = _capture_node  # type: ignore[attr-defined]
    thread_deps.tool_call_recorder = _capture_tool  # type: ignore[attr-defined]

    result: dict[str, Any] = fn(state, thread_deps)  # type: ignore[assignment]
    state_dict: dict[str, Any] = state  # type: ignore[assignment]
    partial = {
        k: v for k, v in result.items() if k not in state_dict or result[k] != state_dict[k]
    }
    return _CollectorResult(
        partial_state=partial,
        node_trace_args=captured_node,
        tool_call_args=captured_tools,
    )
