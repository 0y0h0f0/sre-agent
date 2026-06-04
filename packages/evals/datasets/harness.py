from __future__ import annotations

import json
import os
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver
from pydantic import BaseModel
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from apps.api.schemas.alerts import AlertCreateRequest
from packages.agent.llm import build_llm
from packages.agent.runner import AgentRunner
from packages.agent.schemas import AgentDeps
from packages.common.ids import new_id
from packages.common.settings import Settings
from packages.common.time import utc_now
from packages.db.base import Base
from packages.db.models import AgentRun, AgentRunNode, Incident
from packages.db.repositories.agent_runs import AgentRunRepository
from packages.db.repositories.approvals import ApprovalRepository
from packages.db.repositories.incidents import IncidentRepository
from packages.db.repositories.reports import IncidentReportRepository
from packages.db.repositories.runbooks import RunbookChunkRepository
from packages.db.repositories.tool_calls import ToolCallRepository
from packages.memory.context_builder import ContextBuilder
from packages.memory.memory_store import MemoryStore
from packages.rag.ingest import RunbookIngestor
from packages.rag.retriever import RunbookRetriever
from packages.tools.base import ToolResult, compact_summary, elapsed_ms, start_timer
from packages.tools.cache import RequestLocalToolCache, build_cache_key
from packages.tools.git_changes import GitChangeTool
from packages.tools.logs import LogsQuery
from packages.tools.metrics import MetricsQuery
from packages.tools.runbook_search import RunbookSearchTool
from packages.tools.traces import TraceTool

from .datasets import EvalCase


@dataclass(slots=True)
class EvalCaseResult:
    case_id: str
    incident_type: str
    source_path: str
    incident_id: str
    agent_run_id: str
    status: str
    approval_interrupted: bool
    root_cause_summary: str
    root_cause_hit: bool
    top3_hit: bool
    required_evidence_hit: bool
    expected_risk_level: str
    actual_risk_level: str
    duration_ms: int
    tool_total: int
    tool_successes: int
    tool_cache_hits: int
    prompt_token_estimate: int
    completion_token_estimate: int
    compression_retention_rate: float
    structured_output_valid: bool
    memory_misuse: bool
    report_id: str | None
    report_version: int | None
    error: str | None = None


@dataclass(slots=True)
class EvalSuiteReport:
    suite: str
    dataset_version: str
    git_commit: str
    model_name: str
    prompt_version: str
    started_at: datetime
    finished_at: datetime
    metrics: dict[str, Any]
    cases: list[EvalCaseResult]

    def to_json(self) -> dict[str, Any]:
        return {
            "suite": self.suite,
            "dataset_version": self.dataset_version,
            "git_commit": self.git_commit,
            "model_name": self.model_name,
            "prompt_version": self.prompt_version,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat(),
            "metrics": self.metrics,
            "cases": [asdict_case(case) for case in self.cases],
        }

    def to_markdown(self) -> str:
        lines = [
            f"# Eval Report: {self.suite}",
            "",
            f"- Dataset version: `{self.dataset_version}`",
            f"- Git commit: `{self.git_commit}`",
            f"- Model: `{self.model_name}`",
            f"- Prompt version: `{self.prompt_version}`",
            f"- Run time: `{int((self.finished_at - self.started_at).total_seconds() * 1000)} ms`",
            "",
            "## Metrics",
        ]
        for key, value in self.metrics.items():
            lines.append(f"- {key}: `{value}`")
        lines.extend(
            [
                "",
                "## Cases",
                "",
                "| case | status | top1 | top3 | evidence | approval | risk | duration ms |",
                "| --- | --- | --- | --- | --- | --- | --- | --- |",
            ]
        )
        for case in self.cases:
            case_row = (
                "| {case} | {status} | {top1} | {top3} | {evidence} | "
                "{approval} | {risk} | {duration} |"
            )
            lines.append(
                case_row.format(
                    case=case.case_id,
                    status=case.status,
                    top1="pass" if case.root_cause_hit else "fail",
                    top3="pass" if case.top3_hit else "fail",
                    evidence="pass" if case.required_evidence_hit else "fail",
                    approval="pass"
                    if case.approval_interrupted or case.expected_risk_level in {"L0", "L1"}
                    else "fail",
                    risk=case.actual_risk_level,
                    duration=case.duration_ms,
                )
            )
        return "\n".join(lines)


class _FixtureResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self._payload


class _FixtureMetricsTool:
    name = "metrics"

    def __init__(
        self, fixtures: dict[str, Any], *, cache: RequestLocalToolCache | None = None
    ) -> None:
        self.fixtures = fixtures
        self.timeout_seconds = 2.0
        self.cache = cache

    def run(self, query: BaseModel) -> ToolResult:
        metrics_query = MetricsQuery.model_validate(query)
        started_at = start_timer()
        cache_key = build_cache_key(
            tool_name=self.name,
            service=metrics_query.service,
            query=metrics_query,
            start=metrics_query.start,
            end=metrics_query.end,
            bucket_seconds=60,
        )
        cached = self.cache.get(cache_key) if self.cache else None
        if cached is not None:
            return cached.model_copy(update={"duration_ms": elapsed_ms(started_at)})

        payload = self.fixtures.get(metrics_query.metric_type, {"samples": []})
        samples = [float(value) for value in payload.get("samples", [])]
        if not samples:
            result = ToolResult(
                status="degraded",
                data={"metric_type": metrics_query.metric_type, "samples": []},
                summary=f"no metrics for {metrics_query.service} {metrics_query.metric_type}",
                evidence=[],
                cache_key=cache_key,
                duration_ms=elapsed_ms(started_at),
                error_message="empty metrics fixture",
            )
        else:
            stats = _series_stats(samples)
            result = ToolResult(
                status="succeeded",
                data={
                    "metric_type": metrics_query.metric_type,
                    "service": metrics_query.service,
                    "stats": stats,
                },
                summary=compact_summary(
                    {
                        "service": metrics_query.service,
                        "metric": metrics_query.metric_type,
                        "avg": round(stats["avg"], 4),
                        "p95": round(stats["p95"], 4),
                        "last": round(stats["last"], 4),
                    }
                ),
                evidence=[
                    {
                        "type": "metric",
                        "source": "eval-fixture",
                        "title": f"{metrics_query.metric_type} for {metrics_query.service}",
                        "payload": {
                            "metric_type": metrics_query.metric_type,
                            "samples": samples,
                            "stats": stats,
                        },
                    }
                ],
                cache_key=cache_key,
                duration_ms=elapsed_ms(started_at),
            )
        if self.cache and result.status in {"succeeded", "degraded"}:
            self.cache.set(cache_key, result)
        return result


class _FixtureLogsTool:
    name = "logs"

    def __init__(
        self, fixtures: dict[str, Any], *, cache: RequestLocalToolCache | None = None
    ) -> None:
        self.fixtures = fixtures
        self.timeout_seconds = 2.0
        self.cache = cache

    def run(self, query: BaseModel) -> ToolResult:
        logs_query = LogsQuery.model_validate(query)
        started_at = start_timer()
        cache_key = build_cache_key(
            tool_name=self.name,
            service=logs_query.service,
            query=logs_query,
            start=logs_query.start,
            end=logs_query.end,
            bucket_seconds=60,
        )
        cached = self.cache.get(cache_key) if self.cache else None
        if cached is not None:
            return cached.model_copy(update={"duration_ms": elapsed_ms(started_at)})

        lines = list(self.fixtures.get("lines", []))
        filtered = []
        for line in lines:
            labels = dict(line.get("labels", {}))
            if labels.get("service") not in {None, logs_query.service}:
                continue
            text = str(line.get("line", ""))
            if logs_query.keywords and not any(
                keyword.lower() in text.lower() for keyword in logs_query.keywords
            ):
                continue
            filtered.append(line)

        if not filtered:
            result = ToolResult(
                status="degraded",
                data={"line_count": 0, "error_type_counts": {}, "samples": []},
                summary=f"no logs for {logs_query.service}",
                evidence=[],
                cache_key=cache_key,
                duration_ms=elapsed_ms(started_at),
                error_message="empty logs fixture",
            )
        else:
            parsed = [_parse_log_line(item["line"]) for item in filtered]
            counts = Counter(_error_type(item) for item in parsed)
            payload = {
                "line_count": len(filtered),
                "error_type_counts": dict(counts),
                "top_error_type": counts.most_common(1)[0][0] if counts else None,
                "samples": [
                    {
                        "timestamp": item.get("timestamp"),
                        "message": _parse_log_line(item["line"]).get("message", item["line"]),
                        "level": _parse_log_line(item["line"]).get("level"),
                        "labels": item.get("labels", {}),
                    }
                    for item in filtered[:5]
                ],
            }
            result = ToolResult(
                status="succeeded",
                data=payload,
                summary=compact_summary(
                    {
                        "service": logs_query.service,
                        "lines": len(filtered),
                        "top_error": payload["top_error_type"],
                    }
                ),
                evidence=[
                    {
                        "type": "log",
                        "source": "eval-fixture",
                        "title": f"log samples for {logs_query.service}",
                        "payload": payload,
                    }
                ],
                cache_key=cache_key,
                duration_ms=elapsed_ms(started_at),
            )
        if self.cache and result.status in {"succeeded", "degraded"}:
            self.cache.set(cache_key, result)
        return result


@dataclass(slots=True)
class _EvalEnvironment:
    engine: Any
    session_factory: Any
    session: Session
    settings: Settings
    tool_cache: RequestLocalToolCache
    runbook_path: Path


def run_case(case: EvalCase, *, suite: str, settings: Settings | None = None) -> EvalCaseResult:
    settings = settings or _eval_settings()
    env = _make_environment(settings)
    session = env.session
    try:
        _seed_runbooks(session, env.runbook_path)
        incident = _create_incident(session, case, settings)
        agent_run = _create_agent_run(session, incident.incident_id, settings)
        session.commit()

        deps = _build_deps(session, settings, case, agent_run.agent_run_id, env.tool_cache)
        checkpointer = InMemorySaver()
        runner = AgentRunner(deps, checkpointer=checkpointer)

        initial_result = runner.run(
            incident.incident_id, agent_run.agent_run_id, _alert_payload(case)
        )
        approval_interrupted = initial_result.get("status") == "waiting_approval"
        final_result = initial_result
        if approval_interrupted:
            for _ in range(3):
                final_result = runner.resume(agent_run.agent_run_id, "approved")
                if final_result.get("status") != "waiting_approval":
                    break

        _finalize_run(session, incident.incident_id, agent_run.agent_run_id, final_result)
        session.commit()

        approval_interrupted = bool(
            ApprovalRepository(session).list_for_incident(incident.incident_id)
        )

        state = dict(final_result.get("state", {}))
        root_cause_summary = _string_value(state.get("root_cause", {}), "summary")
        root_cause_hit = _match_keywords(
            root_cause_summary, case.expected.get("root_cause_keywords", [])
        )
        top3_hit = _top3_match(state, case.expected.get("top3_root_causes", []))
        required_evidence_hit = _required_evidence_hit(
            state, case.expected.get("required_evidence_types", [])
        )
        actual_risk_level = _actual_risk_level(state)
        tool_calls = ToolCallRepository(session).list_for_run(agent_run.agent_run_id)
        tool_total = len(tool_calls)
        tool_successes = sum(1 for call in tool_calls if call.status == "succeeded")
        tool_cache_hits = sum(1 for call in tool_calls if call.cache_hit)
        prompt_tokens = int(sum(int(v) for v in (state.get("token_budget") or {}).values()))
        completion_tokens = int(getattr(agent_run, "total_completion_tokens", 0) or 0)
        compression_rate = _compression_retention_rate(state)
        structured_output_valid = _structured_output_valid(state, final_result)
        memory_misuse = _memory_misuse(state, case)
        report = IncidentReportRepository(session).get_latest_for_incident(incident.incident_id)
        duration_ms = _duration_ms(agent_run.created_at, agent_run.updated_at)
        return EvalCaseResult(
            case_id=case.case_id,
            incident_type=case.incident_type,
            source_path=case.source_path,
            incident_id=incident.incident_id,
            agent_run_id=agent_run.agent_run_id,
            status=final_result.get("status", "unknown"),
            approval_interrupted=approval_interrupted,
            root_cause_summary=root_cause_summary,
            root_cause_hit=root_cause_hit,
            top3_hit=top3_hit,
            required_evidence_hit=required_evidence_hit,
            expected_risk_level=str(case.expected.get("expected_risk_level", "unknown")),
            actual_risk_level=actual_risk_level,
            duration_ms=duration_ms,
            tool_total=tool_total,
            tool_successes=tool_successes,
            tool_cache_hits=tool_cache_hits,
            prompt_token_estimate=prompt_tokens,
            completion_token_estimate=completion_tokens,
            compression_retention_rate=compression_rate,
            structured_output_valid=structured_output_valid,
            memory_misuse=memory_misuse,
            report_id=report.report_id if report else None,
            report_version=report.version if report else None,
            error=None
            if final_result.get("status") != "failed"
            else str(final_result.get("error", "failed")),
        )
    finally:
        session.close()
        env.engine.dispose()


def run_suite(suite: str, *, output: str | Path | None = None) -> EvalSuiteReport:
    from .datasets import load_suite_cases, suite_dataset_version

    cases = load_suite_cases(suite)
    started_at = utc_now()
    settings = _eval_settings()
    results = [run_case(case, suite=suite, settings=settings) for case in cases]
    finished_at = utc_now()
    report = EvalSuiteReport(
        suite=suite,
        dataset_version=suite_dataset_version(suite),
        git_commit=_git_commit(),
        model_name=settings.llm_model,
        prompt_version="v1",
        started_at=started_at,
        finished_at=finished_at,
        metrics=_suite_metrics(results),
        cases=results,
    )

    output_path = Path(output) if output is not None else Path("reports") / f"eval-{suite}.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(report.to_json(), indent=2, ensure_ascii=True), encoding="utf-8"
    )
    output_path.with_suffix(".md").write_text(report.to_markdown() + "\n", encoding="utf-8")
    return report


def _make_environment(settings: Settings) -> _EvalEnvironment:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(
        bind=engine, autoflush=False, autocommit=False, expire_on_commit=False
    )
    session = session_factory()
    return _EvalEnvironment(
        engine=engine,
        session_factory=session_factory,
        session=session,
        settings=settings,
        tool_cache=RequestLocalToolCache(),
        runbook_path=Path("demo/runbooks"),
    )


def _seed_runbooks(session: Session, runbook_path: Path) -> None:
    ingestor = RunbookIngestor(RunbookChunkRepository(session))
    ingestor.ingest_path(runbook_path, reingest=True)
    session.commit()


def _build_deps(
    session: Session,
    settings: Settings,
    case: EvalCase,
    agent_run_id: str,
    tool_cache: RequestLocalToolCache,
) -> AgentDeps:
    chunk_repo = RunbookChunkRepository(session)
    retriever = RunbookRetriever(chunk_repo)
    return AgentDeps(
        db=session,
        settings=settings,
        tool_cache=tool_cache,
        metrics_tool=_FixtureMetricsTool(case.fixtures["metrics"], cache=tool_cache),
        logs_tool=_FixtureLogsTool(case.fixtures["logs"], cache=tool_cache),
        trace_tool=TraceTool(
            fixture_path=case.fixtures["traces"],
            timeout_seconds=settings.tool_timeout_seconds,
            cache=tool_cache,
        ),
        git_change_tool=GitChangeTool(
            fixture_path=case.fixtures["git_changes"],
            timeout_seconds=settings.tool_timeout_seconds,
            cache=tool_cache,
        ),
        runbook_search_tool=RunbookSearchTool(
            retriever=retriever, timeout_seconds=settings.tool_timeout_seconds, cache=tool_cache
        ),
        memory_store=MemoryStore(session),
        context_builder=ContextBuilder(),
        llm=build_llm(settings),
        node_tracer=_node_tracer(session, agent_run_id),
        tool_call_recorder=_tool_call_recorder(session, agent_run_id),
    )


def _create_incident(session: Session, case: EvalCase, settings: Settings) -> Incident:
    repo = IncidentRepository(session)
    payload = AlertCreateRequest.model_validate(case.alert)
    incident_id = new_id("inc_")
    return repo.create(incident_id, payload)


def _create_agent_run(session: Session, incident_id: str, settings: Settings) -> AgentRun:
    repo = AgentRunRepository(session)
    agent_run_id = new_id("run_")
    run = repo.create(agent_run_id, incident_id, model_name=settings.llm_model)
    return run


def _alert_payload(case: EvalCase) -> dict[str, Any]:
    return dict(case.alert)


def _finalize_run(
    session: Session, incident_id: str, agent_run_id: str, result: dict[str, Any]
) -> None:
    runs = AgentRunRepository(session)
    incidents = IncidentRepository(session)
    run = runs.get_by_public_id(agent_run_id)
    incident = incidents.get_by_public_id(incident_id)
    if run is None or incident is None:
        return
    state = _sanitize_state(dict(result.get("state", {})))
    if result.get("status") == "succeeded":
        runs.mark_succeeded(run, state)
        incident.status = "mitigated" if state.get("execution_results") else "resolved"
    elif result.get("status") == "failed":
        runs.mark_failed(agent_run_id, "EVAL_FAILED", str(result.get("error", "unknown")))
        incident.status = "failed"
    elif result.get("status") == "waiting_approval":
        run.status = "waiting_approval"
        run.state = state
        incident.status = "waiting_approval"


def _node_tracer(session: Session, agent_run_id: str) -> Callable[..., None]:
    def tracer(**kwargs: Any) -> None:
        started_at = kwargs.get("started_at")
        finished_at = kwargs.get("finished_at")
        duration_ms = 0
        if started_at is not None and finished_at is not None:
            duration_ms = max(0, int((finished_at - started_at).total_seconds() * 1000))
        node = AgentRunNode(
            node_id=kwargs.get("node_id", new_id("nd_")),
            agent_run_id=kwargs.get("agent_run_id", agent_run_id),
            name=kwargs.get("name", "unknown"),
            status=kwargs.get("status", "unknown"),
            started_at=started_at,
            finished_at=finished_at,
            duration_ms=duration_ms,
            input_summary=(kwargs.get("input_summary") or "")[:500],
            output_summary=(kwargs.get("output_summary") or "")[:500],
            error_message=(kwargs.get("error_message") or "")[:500] or None,
        )
        session.add(node)
        session.flush()

    return tracer


def _tool_call_recorder(session: Session, agent_run_id: str) -> Callable[..., None]:
    repo = ToolCallRepository(session)

    def recorder(**kwargs: Any) -> None:
        repo.create(
            agent_run_id=kwargs.get("agent_run_id", agent_run_id),
            node_name=kwargs.get("node_name", "unknown"),
            tool_name=kwargs.get("tool_name", "unknown"),
            query=kwargs.get("query", {}),
            result=kwargs.get(
                "result", ToolResult(status="degraded", data={}, summary="", duration_ms=0)
            ),
            input_summary=kwargs.get("input_summary", ""),
        )
        session.flush()

    return recorder


def _suite_metrics(results: list[EvalCaseResult]) -> dict[str, Any]:
    total = len(results) or 1
    high_risk_cases = [case for case in results if case.expected_risk_level in {"L2", "L3"}]
    tool_total = max(sum(case.tool_total for case in results), 1)
    return {
        "case_count": len(results),
        "root_cause_top1_hit_rate": round(
            sum(1 for case in results if case.root_cause_hit) / total, 4
        ),
        "root_cause_top3_hit_rate": round(sum(1 for case in results if case.top3_hit) / total, 4),
        "required_evidence_coverage": round(
            sum(1 for case in results if case.required_evidence_hit) / total, 4
        ),
        "high_risk_interception_rate": round(
            sum(1 for case in high_risk_cases if case.approval_interrupted)
            / (len(high_risk_cases) or 1),
            4,
        ),
        "json_valid_rate": round(
            sum(1 for case in results if case.structured_output_valid) / total, 4
        ),
        "report_generation_rate": round(
            sum(1 for case in results if case.report_id is not None) / total, 4
        ),
        "avg_duration_ms": round(sum(case.duration_ms for case in results) / total, 1),
        "avg_prompt_token_estimate": round(
            sum(case.prompt_token_estimate for case in results) / total, 1
        ),
        "avg_completion_token_estimate": round(
            sum(case.completion_token_estimate for case in results) / total, 1
        ),
        "p95_prompt_token_estimate": _p95([case.prompt_token_estimate for case in results]),
        "tool_success_rate": round(sum(case.tool_successes for case in results) / tool_total, 4),
        "tool_cache_hit_rate": round(sum(case.tool_cache_hits for case in results) / tool_total, 4),
        "provider_prompt_cache_hit_rate": "unknown",
        "app_prompt_segment_cache_hit_rate": "unknown",
        "compression_retention_rate": round(
            sum(case.compression_retention_rate for case in results) / total,
            4,
        ),
        "memory_misuse_rate": round(sum(1 for case in results if case.memory_misuse) / total, 4),
    }


def _p95(values: list[int]) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    index = max(0, int(len(ordered) * 0.95) - 1)
    return ordered[index]


def _compression_retention_rate(state: dict[str, Any]) -> float:
    events = state.get("compression_events") or []
    if not isinstance(events, list) or not events:
        return 1.0
    total_before = 0
    total_after = 0
    for event in events:
        if not isinstance(event, dict):
            continue
        total_before += int(event.get("before_tokens", 0) or 0)
        total_after += int(event.get("after_tokens", 0) or 0)
    if total_before <= 0:
        return 1.0
    return round(total_after / total_before, 4)


def _structured_output_valid(state: dict[str, Any], result: dict[str, Any]) -> bool:
    if result.get("status") == "failed":
        return False
    root_cause = state.get("root_cause")
    hypotheses = state.get("hypotheses")
    actions = state.get("recommended_actions")
    return (
        isinstance(root_cause, dict)
        and isinstance(root_cause.get("summary"), str)
        and bool(root_cause.get("summary"))
        and isinstance(hypotheses, list)
        and isinstance(actions, list)
    )


def _memory_misuse(state: dict[str, Any], case: EvalCase) -> bool:
    memories = [item for item in state.get("memory_context", []) if isinstance(item, dict)]
    if not memories:
        return False
    expected_service = str(case.alert.get("service", "")).lower()
    for memory in memories:
        content = str(memory.get("content", "")).lower()
        source_ref = str(memory.get("source_ref", "")).lower()
        if (
            expected_service
            and expected_service not in content
            and expected_service not in source_ref
        ):
            return True
    return False


def _required_evidence_hit(state: dict[str, Any], expected_types: list[str]) -> bool:
    if not expected_types:
        return True
    present = _evidence_types(state)
    return all(expected_type in present for expected_type in expected_types)


def _evidence_types(state: dict[str, Any]) -> set[str]:
    types: set[str] = set()
    for key in ("metrics_evidence", "logs_evidence", "traces_evidence", "deployment_evidence"):
        for item in state.get(key, []) or []:
            if isinstance(item, dict) and item.get("type"):
                types.add(str(item["type"]))
    for item in state.get("runbook_context", []) or []:
        if isinstance(item, dict) and (item.get("chunk_id") or item.get("source_path")):
            types.add("runbook")
    return types


def _top3_match(state: dict[str, Any], expected_candidates: list[str]) -> bool:
    if not expected_candidates:
        return True
    observed = [
        _string_value(state.get("root_cause", {}), "summary"),
        *[
            str(item.get("statement", ""))
            for item in state.get("hypotheses", [])
            if isinstance(item, dict)
        ],
    ]
    normalized = " | ".join(text.lower() for text in observed if text)
    return any(candidate.lower() in normalized for candidate in expected_candidates)


def _match_keywords(summary: str, keywords: list[str]) -> bool:
    normalized = summary.lower()
    return bool(keywords) and all(keyword.lower() in normalized for keyword in keywords)


def _actual_risk_level(state: dict[str, Any]) -> str:
    order = {"L0": 0, "L1": 1, "L2": 2, "L3": 3, "L4": 4}
    actions = [
        action for action in state.get("recommended_actions", []) if isinstance(action, dict)
    ]
    if not actions:
        return "unknown"
    return max(
        (str(action.get("risk_level", "L0")) for action in actions),
        key=lambda item: order.get(item, -1),
    )


def _string_value(source: dict[str, Any], key: str) -> str:
    value = source.get(key)
    return value if isinstance(value, str) else ""


def _sanitize_state(state: dict[str, Any]) -> dict[str, Any]:
    def convert(value: Any) -> Any:
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, dict):
            return {k: convert(v) for k, v in value.items() if not str(k).startswith("_")}
        if isinstance(value, list):
            return [convert(v) for v in value]
        return value

    converted = convert(state)
    return converted if isinstance(converted, dict) else {}


def _parse_log_line(line: str) -> dict[str, Any]:
    try:
        payload = json.loads(line)
        return payload if isinstance(payload, dict) else {"message": line}
    except json.JSONDecodeError:
        return {"message": line}


def _error_type(parsed: dict[str, Any]) -> str:
    for key in ("error_type", "exception", "event"):
        value = parsed.get(key)
        if isinstance(value, str) and value:
            return value
    message = str(parsed.get("message", "")).lower()
    if "timeout" in message:
        return "timeout"
    if "connection" in message and ("exhaust" in message or "refused" in message):
        return "connection_error"
    if "redis" in message or "cache" in message:
        return "cache_error"
    if "oom" in message or "restart" in message:
        return "pod_restart"
    if "5xx" in message or "http 500" in message:
        return "http_5xx"
    return str(parsed.get("level") or "unknown")


def _series_stats(values: list[float]) -> dict[str, float]:
    ordered = list(values)
    sorted_values = sorted(ordered)
    index = max(0, int(len(sorted_values) * 0.95) - 1)
    first = ordered[0]
    last = ordered[-1]
    change_ratio = 0.0 if first == 0 else (last - first) / abs(first)
    return {
        "min": min(ordered),
        "max": max(ordered),
        "avg": sum(ordered) / len(ordered),
        "p95": sorted_values[index],
        "first": first,
        "last": last,
        "change_ratio": change_ratio,
    }


def _duration_ms(started_at: datetime, finished_at: datetime) -> int:
    return max(0, int((finished_at - started_at).total_seconds() * 1000))


def _git_commit() -> str:
    import subprocess

    try:
        result = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], cwd=Path(__file__).resolve().parents[2]
        )
        return result.decode("utf-8").strip() or "unknown"
    except Exception:
        return "unknown"


def _eval_settings() -> Settings:
    """Build eval settings.

    Defaults to the deterministic ``fake`` provider so the standard test suite
    stays offline and reproducible. A real-provider smoke is opt-in via env:
    set ``LLM_PROVIDER`` (e.g. ``deepseek``) plus ``LLM_API_KEY`` / ``LLM_MODEL``
    / ``LLM_BASE_URL`` and any reasoning flags. When unset, behavior is unchanged.
    """
    provider = os.getenv("LLM_PROVIDER", "fake").strip().lower()
    overrides: dict[str, Any] = {
        "database_url": "sqlite+pysqlite:///:memory:",
        "redis_url": "memory://redis",
        "celery_broker_url": "memory://broker",
        "celery_result_backend": "memory://backend",
        "llm_provider": provider,
        "llm_model": os.getenv("LLM_MODEL", "fake-diagnosis-model"),
        "trace_fixture_path": "demo/faults/traces.json",
        "git_changes_fixture_path": "demo/faults/git_changes.json",
    }
    if provider != "fake":
        # Honor real-provider config from the environment. llm_model has no useful
        # fake default for a live run, so require it explicitly.
        if not os.getenv("LLM_MODEL"):
            raise ValueError("LLM_MODEL must be set when LLM_PROVIDER is not 'fake'")
        if base_url := os.getenv("LLM_BASE_URL"):
            overrides["llm_base_url"] = base_url
        if api_key := os.getenv("LLM_API_KEY"):
            overrides["llm_api_key"] = api_key
        if (reasoning := os.getenv("LLM_REASONING_ENABLED")) is not None:
            overrides["llm_reasoning_enabled"] = reasoning.strip().lower() in {"1", "true", "yes"}
        if effort := os.getenv("LLM_REASONING_EFFORT"):
            overrides["llm_reasoning_effort"] = effort
        if nodes := os.getenv("LLM_REASONING_NODES"):
            overrides["llm_reasoning_nodes"] = nodes
        if max_tokens := os.getenv("LLM_MAX_TOKENS"):
            overrides["llm_max_tokens"] = int(max_tokens)
        if timeout := os.getenv("LLM_TIMEOUT_SECONDS"):
            overrides["llm_timeout_seconds"] = float(timeout)
    return Settings(**overrides)


def asdict_case(case: EvalCaseResult) -> dict[str, Any]:
    return {
        "case_id": case.case_id,
        "incident_type": case.incident_type,
        "source_path": case.source_path,
        "incident_id": case.incident_id,
        "agent_run_id": case.agent_run_id,
        "status": case.status,
        "approval_interrupted": case.approval_interrupted,
        "root_cause_summary": case.root_cause_summary,
        "root_cause_hit": case.root_cause_hit,
        "top3_hit": case.top3_hit,
        "required_evidence_hit": case.required_evidence_hit,
        "expected_risk_level": case.expected_risk_level,
        "actual_risk_level": case.actual_risk_level,
        "duration_ms": case.duration_ms,
        "tool_total": case.tool_total,
        "tool_successes": case.tool_successes,
        "tool_cache_hits": case.tool_cache_hits,
        "prompt_token_estimate": case.prompt_token_estimate,
        "completion_token_estimate": case.completion_token_estimate,
        "compression_retention_rate": case.compression_retention_rate,
        "structured_output_valid": case.structured_output_valid,
        "memory_misuse": case.memory_misuse,
        "report_id": case.report_id,
        "report_version": case.report_version,
        "error": case.error,
    }
