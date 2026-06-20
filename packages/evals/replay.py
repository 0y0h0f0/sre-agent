"""Offline replay engine — rerun historical alerts through current prompts.

Replay reads historical incidents from the application database, but it runs
the Agent in an isolated in-memory database so historical replay never creates
real actions, approvals, reports, or tool-call rows for production incidents.
Only the caller may choose to persist the final replay metrics.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from packages.agent.llm import build_llm
from packages.agent.runner import AgentRunner
from packages.agent.schemas import AgentDeps
from packages.common.ids import new_id
from packages.common.settings import Settings
from packages.common.time import utc_now
from packages.db.base import Base
from packages.db.models import AgentRun, Incident, RunbookChunk
from packages.db.repositories.agent_runs import AgentRunRepository
from packages.db.repositories.incidents import IncidentRepository
from packages.db.repositories.reports import IncidentReportRepository
from packages.db.repositories.runbooks import RunbookChunkRepository
from packages.db.repositories.tool_calls import ToolCallRepository
from packages.evals.datasets import harness as eval_harness
from packages.evals.datasets.harness import EvalCaseResult, EvalSuiteReport
from packages.memory.context_builder import ContextBuilder
from packages.memory.memory_store import MemoryStore
from packages.rag.embedding_factory import FakeEmbeddingProvider
from packages.rag.ingest import RunbookIngestor
from packages.rag.reranker_backends import FakeRerankerBackend
from packages.rag.retriever import RunbookRetriever
from packages.tools import (
    DbDiagnosticsTool,
    GitChangeTool,
    K8sDiagnosticsTool,
    LogsTool,
    MetricsTool,
    TraceTool,
    build_db_diagnostics_backend,
    build_deployment_backend,
    build_k8s_backend,
    build_trace_backend,
)
from packages.tools.cache import RequestLocalToolCache
from packages.tools.executor_backends import FixtureExecutorBackend
from packages.tools.runbook_search import RunbookSearchTool

REPLAY_DATASET_VERSION = "historical-db-v1"
_REPLAY_TERMINAL_STATUSES = {"resolved", "mitigated", "failed"}
_MAX_REPLAY_LOOKAHEAD_FACTOR = 5
_SUMMARY_SIMILARITY_THRESHOLD = 0.6


@dataclass(slots=True)
class ReplayTarget:
    # Snapshot just enough historical incident/run data to replay in a detached
    # SQLite database. The source DB remains the read-only reference.
    incident_id: str
    original_agent_run_id: str
    original_summary: str
    source: str
    fingerprint: str
    service: str
    severity: str
    alert_name: str
    status: str
    starts_at: datetime
    ends_at: datetime | None
    labels: dict[str, Any]
    annotations: dict[str, Any]
    raw_payload: dict[str, Any]
    root_cause_summary: str | None
    created_at: datetime


def replay_incident(
    db: Session,
    incident_id: str,
    settings: Settings,
    prompt_version: str = "v1",
    model: str | None = None,
) -> EvalCaseResult | None:
    """Replay a single historical incident and compare to original diagnosis."""
    target, _skipped = _target_for_incident(db, incident_id)
    if target is None:
        return None
    return _run_replay_target(db, target, settings, prompt_version=prompt_version, model=model)


def run_replay_suite(
    db: Session,
    settings: Settings,
    *,
    limit: int = 20,
    service: str | None = None,
    incident_ids: list[str] | None = None,
    model: str | None = None,
    prompt_version: str = "v1",
) -> EvalSuiteReport:
    """Replay recent historical incidents and return an eval-style report.

    Source ``db`` is read-only for historical records. Each case runs in an
    in-memory clone with a forced fixture executor, so replay can be used safely
    against production data without creating real remediation side effects.
    """
    # Bound user-controlled limits before querying history so the API path cannot
    # accidentally trigger a very large replay job.
    bounded_limit = min(max(limit, 1), 100)
    started_at = utc_now()
    targets, skipped = select_replay_targets(
        db,
        limit=bounded_limit,
        service=service,
        incident_ids=incident_ids or [],
    )
    results: list[EvalCaseResult] = []
    for target in targets:
        try:
            results.append(
                _run_replay_target(
                    db,
                    target,
                    settings,
                    prompt_version=prompt_version,
                    model=model,
                )
            )
        except Exception as exc:
            results.append(_failed_result(target, str(exc)))

    finished_at = utc_now()
    effective_settings = _settings_for_replay(settings, model=model)
    return EvalSuiteReport(
        suite="replay",
        dataset_version=REPLAY_DATASET_VERSION,
        git_commit=eval_harness._git_commit(),
        model_name=effective_settings.llm_model,
        prompt_version=prompt_version,
        started_at=started_at,
        finished_at=finished_at,
        metrics=_replay_metrics(
            results,
            selected_count=len(targets),
            skipped=skipped,
            limit=bounded_limit,
            service=service,
            incident_ids=incident_ids or [],
        ),
        cases=results,
    )


def select_replay_targets(
    db: Session,
    *,
    limit: int,
    service: str | None = None,
    incident_ids: list[str] | None = None,
) -> tuple[list[ReplayTarget], list[dict[str, str]]]:
    """Return replayable historical incidents and skipped reasons."""
    skipped: list[dict[str, str]] = []
    if incident_ids:
        # Explicit IDs preserve request order and report not-found entries
        # instead of silently substituting other recent incidents.
        incident_map = {
            item.incident_id: item
            for item in db.scalars(
                select(Incident).where(Incident.incident_id.in_(incident_ids))
            ).all()
        }
        candidates = []
        for incident_id in incident_ids[:limit]:
            incident = incident_map.get(incident_id)
            if incident is None:
                skipped.append({"incident_id": incident_id, "reason": "incident_not_found"})
                continue
            candidates.append(incident)
    else:
        # Look ahead beyond the requested limit because some terminal incidents
        # may lack a usable original diagnosis and will be skipped.
        stmt = (
            select(Incident)
            .where(Incident.status.in_(sorted(_REPLAY_TERMINAL_STATUSES)))
            .order_by(Incident.created_at.desc(), Incident.id.desc())
            .limit(limit * _MAX_REPLAY_LOOKAHEAD_FACTOR)
        )
        if service:
            stmt = stmt.where(Incident.service == service)
        candidates = list(db.scalars(stmt).all())

    targets: list[ReplayTarget] = []
    runs_repo = AgentRunRepository(db)
    for incident in candidates:
        if len(targets) >= limit:
            break
        target, reason = _target_from_incident(incident, runs_repo)
        if target is None:
            skipped.append({"incident_id": incident.incident_id, "reason": reason})
            continue
        targets.append(target)
    return targets, skipped


def _target_for_incident(
    db: Session,
    incident_id: str,
) -> tuple[ReplayTarget | None, str]:
    incident = IncidentRepository(db).get_by_public_id(incident_id)
    if incident is None:
        return None, "incident_not_found"
    return _target_from_incident(incident, AgentRunRepository(db))


def _target_from_incident(
    incident: Incident,
    runs_repo: AgentRunRepository,
) -> tuple[ReplayTarget | None, str]:
    original_run = runs_repo.get_latest_for_incident(incident.incident_id)
    if original_run is None:
        return None, "agent_run_not_found"
    original_summary = _root_cause_summary(original_run.state or {})
    if not original_summary:
        # Replay drift only has meaning when there is an original diagnosis to
        # compare against, so skip incidents without a persisted root cause.
        return None, "original_root_cause_missing"
    return (
        ReplayTarget(
            incident_id=incident.incident_id,
            original_agent_run_id=original_run.agent_run_id,
            original_summary=original_summary,
            source=incident.source,
            fingerprint=incident.fingerprint,
            service=incident.service,
            severity=incident.severity,
            alert_name=incident.alert_name,
            status=incident.status,
            starts_at=incident.starts_at,
            ends_at=incident.ends_at,
            labels=dict(incident.labels or {}),
            annotations=dict(incident.annotations or {}),
            raw_payload=dict(incident.raw_payload or {}),
            root_cause_summary=incident.root_cause_summary,
            created_at=incident.created_at,
        ),
        "",
    )


def _run_replay_target(
    source_db: Session,
    target: ReplayTarget,
    settings: Settings,
    *,
    prompt_version: str,
    model: str | None,
) -> EvalCaseResult:
    # All replay writes happen inside this throwaway SQLite database. The source
    # session is only used to read runbooks and historical incident snapshots.
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(
        bind=engine, autoflush=False, autocommit=False, expire_on_commit=False
    )
    started_at = utc_now()
    effective_settings = _settings_for_replay(settings, model=model)
    try:
        with SessionLocal() as replay_db:
            _seed_replay_runbooks(source_db, replay_db)
            _clone_incident(replay_db, target)
            agent_run = _create_replay_agent_run(
                replay_db,
                target,
                effective_settings,
                prompt_version=prompt_version,
            )
            replay_db.commit()

            cache = RequestLocalToolCache()
            # Build fresh deps per replay case so tool cache, memory, and node
            # traces cannot leak between historical incidents.
            deps = _build_replay_deps(
                replay_db,
                effective_settings,
                agent_run.agent_run_id,
                cache,
            )
            runner = AgentRunner(deps, checkpointer=InMemorySaver())
            result = runner.run(
                target.incident_id,
                agent_run.agent_run_id,
                _alert_payload(target),
            )
            eval_harness._finalize_run(
                replay_db,
                target.incident_id,
                agent_run.agent_run_id,
                result,
            )
            replay_db.commit()

            state = dict(result.get("state", {}))
            summary = _root_cause_summary(state)
            # Replay uses similarity, not exact string matching, because the goal
            # is to detect diagnosis drift while allowing prompt wording changes.
            root_cause_hit = _summary_similarity(summary, target.original_summary) >= (
                _SUMMARY_SIMILARITY_THRESHOLD
            )
            top3_hit = _top3_replay_hit(state, target.original_summary)
            tool_calls = ToolCallRepository(replay_db).list_for_run(agent_run.agent_run_id)
            report = IncidentReportRepository(replay_db).get_latest_for_incident(
                target.incident_id
            )
            finished_at = utc_now()
            return EvalCaseResult(
                case_id=target.incident_id,
                incident_type=target.alert_name,
                source_path=f"replay:{target.incident_id}",
                incident_id=target.incident_id,
                agent_run_id=agent_run.agent_run_id,
                status=str(result.get("status", "unknown")),
                approval_interrupted=result.get("status") == "waiting_approval",
                root_cause_summary=summary,
                root_cause_hit=root_cause_hit,
                top3_hit=top3_hit,
                required_evidence_hit=_has_evidence(state),
                expected_risk_level="historical",
                actual_risk_level=eval_harness._actual_risk_level(state),
                duration_ms=eval_harness._duration_ms(started_at, finished_at),
                tool_total=len(tool_calls),
                tool_successes=sum(1 for call in tool_calls if call.status == "succeeded"),
                tool_cache_hits=sum(1 for call in tool_calls if call.cache_hit),
                prompt_token_estimate=_prompt_tokens(state),
                completion_token_estimate=int(agent_run.total_completion_tokens or 0),
                compression_retention_rate=eval_harness._compression_retention_rate(state),
                structured_output_valid=_structured_output_valid(state, result),
                memory_misuse=False,
                report_id=report.report_id if report else None,
                report_version=report.version if report else None,
                error=None if result.get("status") != "failed" else str(result.get("error")),
            )
    finally:
        engine.dispose()


def _settings_for_replay(settings: Settings, *, model: str | None) -> Settings:
    updates: dict[str, Any] = {
        # Even when replay reads live diagnostic backends, execution and semantic
        # retrieval stay deterministic and non-mutating.
        "executor_backend": "fixture",
        "embedding_provider": "fake",
        "reranker_provider": "fake",
    }
    if model:
        updates["llm_model"] = model
    return settings.model_copy(update=updates)


def _build_replay_deps(
    db: Session,
    settings: Settings,
    agent_run_id: str,
    cache: RequestLocalToolCache,
) -> AgentDeps:
    # Replay intentionally uses the configured read adapters for metrics/logs/
    # traces/K8s/DB, but forces remediation through FixtureExecutorBackend below.
    retriever = RunbookRetriever(
        RunbookChunkRepository(db),
        embedding_provider=FakeEmbeddingProvider(),
        reranker=FakeRerankerBackend(),
        use_hybrid=settings.runbook_hybrid_search_enabled,
    )
    return AgentDeps(
        db=db,
        settings=settings,
        tool_cache=cache,
        metrics_tool=MetricsTool(
            base_url=settings.prometheus_url,
            timeout_seconds=settings.tool_timeout_seconds,
            cache=cache,
            service_label=settings.metrics_service_label,
            step_seconds=settings.metrics_step_seconds,
            max_window_seconds=settings.metrics_max_window_seconds,
            max_shards=settings.metrics_max_shards,
        ),
        logs_tool=LogsTool(
            base_url=settings.loki_url,
            timeout_seconds=settings.tool_timeout_seconds,
            cache=cache,
            service_label=settings.logs_service_label,
        ),
        trace_tool=TraceTool(
            backend=build_trace_backend(settings),
            timeout_seconds=settings.tool_timeout_seconds,
            cache=cache,
        ),
        git_change_tool=GitChangeTool(
            backend=build_deployment_backend(settings),
            timeout_seconds=settings.tool_timeout_seconds,
            cache=cache,
        ),
        k8s_tool=K8sDiagnosticsTool(
            backend=build_k8s_backend(settings),
            timeout_seconds=settings.tool_timeout_seconds,
        ),
        db_diagnostics_tool=DbDiagnosticsTool(
            backend=build_db_diagnostics_backend(settings),
            timeout_seconds=settings.tool_timeout_seconds,
        ),
        runbook_search_tool=RunbookSearchTool(
            retriever=retriever,
            timeout_seconds=settings.tool_timeout_seconds,
            cache=cache,
        ),
        memory_store=MemoryStore(db),
        context_builder=ContextBuilder(),
        llm=build_llm(settings),
        node_tracer=eval_harness._node_tracer(db, agent_run_id),
        tool_call_recorder=eval_harness._tool_call_recorder(db, agent_run_id),
        executor_backend=FixtureExecutorBackend(),
    )


def _clone_incident(db: Session, target: ReplayTarget) -> None:
    # Reuse the public incident ID in the clone so agent state and reports remain
    # comparable to the source incident, while the row lives only in replay_db.
    db.add(
        Incident(
            incident_id=target.incident_id,
            fingerprint=target.fingerprint,
            source=target.source,
            service=target.service,
            severity=target.severity,
            alert_name=target.alert_name,
            status=target.status,
            starts_at=target.starts_at,
            ends_at=target.ends_at,
            labels=dict(target.labels),
            annotations=dict(target.annotations),
            raw_payload=dict(target.raw_payload),
            root_cause_summary=target.root_cause_summary,
            created_at=target.created_at,
        )
    )


def _create_replay_agent_run(
    db: Session,
    target: ReplayTarget,
    settings: Settings,
    *,
    prompt_version: str,
) -> AgentRun:
    agent_run_id = new_id("run_")
    # Replay runs use a normal checkpoint identity, but the InMemorySaver in the
    # caller keeps checkpoint writes inside the temporary process.
    run = AgentRun(
        agent_run_id=agent_run_id,
        incident_id=target.incident_id,
        status="queued",
        model_name=settings.llm_model,
        prompt_version=prompt_version,
        state={},
        checkpoint_thread_id=agent_run_id,
        checkpoint_ns="",
    )
    db.add(run)
    return run


def _seed_replay_runbooks(source_db: Session, replay_db: Session) -> None:
    chunks = list(RunbookChunkRepository(source_db).list_chunks())
    if chunks:
        # Prefer copying source runbooks so replay evaluates against knowledge
        # that was actually available in the application database.
        for chunk in chunks:
            replay_db.add(
                RunbookChunk(
                    chunk_id=chunk.chunk_id,
                    document_id=chunk.document_id,
                    source_path=chunk.source_path,
                    title=chunk.title,
                    content=chunk.content,
                    content_hash=chunk.content_hash,
                    embedding=_safe_embedding(chunk.embedding),
                    embedding_model=chunk.embedding_model,
                    tsv_content=None,
                    language=chunk.language,
                    metadata_json=dict(chunk.metadata_json or {}),
                    created_at=chunk.created_at,
                    updated_at=chunk.updated_at,
                )
            )
        replay_db.flush()
        return

    demo_runbooks = Path("demo/runbooks")
    if demo_runbooks.exists():
        # Empty source DBs still get deterministic demo runbooks, which keeps
        # local replay usable immediately after bootstrapping.
        RunbookIngestor(
            RunbookChunkRepository(replay_db),
            embedding_provider=FakeEmbeddingProvider(),
        ).ingest_path(demo_runbooks, reingest=True)
        replay_db.flush()


def _safe_embedding(value: Any) -> list[float]:
    # SQLite replay does not enforce pgvector, but downstream code expects the
    # current 512-dimensional embedding shape.
    if isinstance(value, list) and len(value) == 512:
        return [float(item) for item in value]
    return [0.0] * 512


def _alert_payload(target: ReplayTarget) -> dict[str, Any]:
    return {
        "source": target.source,
        "fingerprint": target.fingerprint,
        "service": target.service,
        "severity": target.severity,
        "alert_name": target.alert_name,
        "starts_at": target.starts_at,
        "ends_at": target.ends_at,
        "labels": dict(target.labels),
        "annotations": dict(target.annotations),
        "raw_payload": dict(target.raw_payload),
    }


def _root_cause_summary(state: dict[str, Any]) -> str:
    root_cause = state.get("root_cause")
    if isinstance(root_cause, dict) and isinstance(root_cause.get("summary"), str):
        return str(root_cause["summary"]).strip()
    report = state.get("incident_report")
    if isinstance(report, dict) and isinstance(report.get("root_cause"), str):
        return str(report["root_cause"]).strip()
    return ""


def _top3_replay_hit(state: dict[str, Any], original_summary: str) -> bool:
    observed = [_root_cause_summary(state)]
    for hypothesis in state.get("hypotheses", []) or []:
        if isinstance(hypothesis, dict):
            observed.append(str(hypothesis.get("statement", "")))
    return any(
        _summary_similarity(summary, original_summary) >= _SUMMARY_SIMILARITY_THRESHOLD
        for summary in observed
    )


def _summary_similarity(left: str, right: str) -> float:
    left_norm = _normalize_summary(left)
    right_norm = _normalize_summary(right)
    if not left_norm or not right_norm:
        return 0.0
    if left_norm == right_norm:
        return 1.0
    left_tokens = _summary_tokens(left_norm)
    right_tokens = _summary_tokens(right_norm)
    # Token Jaccard is useful for longer English-style summaries; character
    # bigrams give a fallback for short labels or mixed-language root causes.
    if len(left_tokens) >= 3 and len(right_tokens) >= 3:
        return _jaccard(left_tokens, right_tokens)
    return _jaccard(_char_bigrams(left_norm), _char_bigrams(right_norm))


def _normalize_summary(value: str) -> str:
    return " ".join(str(value or "").lower().split())


def _summary_tokens(value: str) -> set[str]:
    stopwords = {
        "a",
        "an",
        "and",
        "after",
        "are",
        "in",
        "is",
        "of",
        "the",
        "to",
        "with",
    }
    return {
        token
        for token in re.findall(r"[a-z0-9_]+", value.lower())
        if len(token) > 2 and token not in stopwords
    }


def _char_bigrams(value: str) -> set[str]:
    compact = re.sub(r"\s+", "", value)
    if len(compact) < 2:
        return {compact} if compact else set()
    return {compact[index : index + 2] for index in range(len(compact) - 1)}


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _has_evidence(state: dict[str, Any]) -> bool:
    if state.get("evidence_ids"):
        return True
    for key in (
        "metrics_evidence",
        "logs_evidence",
        "traces_evidence",
        "deployment_evidence",
        "k8s_evidence",
        "db_evidence",
        "runbook_context",
    ):
        value = state.get(key)
        if isinstance(value, list) and value:
            return True
    return False


def _structured_output_valid(state: dict[str, Any], result: dict[str, Any]) -> bool:
    if result.get("status") == "failed":
        return False
    root_cause = state.get("root_cause")
    return (
        isinstance(root_cause, dict)
        and isinstance(root_cause.get("summary"), str)
        and bool(str(root_cause.get("summary")).strip())
        and isinstance(state.get("hypotheses"), list)
        and isinstance(state.get("recommended_actions"), list)
    )


def _prompt_tokens(state: dict[str, Any]) -> int:
    token_budget = state.get("token_budget")
    if not isinstance(token_budget, dict):
        return 0
    return int(sum(int(value or 0) for value in token_budget.values()))


def _failed_result(target: ReplayTarget, error: str) -> EvalCaseResult:
    return EvalCaseResult(
        case_id=target.incident_id,
        incident_type=target.alert_name,
        source_path=f"replay:{target.incident_id}",
        incident_id=target.incident_id,
        agent_run_id="",
        status="failed",
        approval_interrupted=False,
        root_cause_summary="",
        root_cause_hit=False,
        top3_hit=False,
        required_evidence_hit=False,
        expected_risk_level="historical",
        actual_risk_level="unknown",
        duration_ms=0,
        tool_total=0,
        tool_successes=0,
        tool_cache_hits=0,
        prompt_token_estimate=0,
        completion_token_estimate=0,
        compression_retention_rate=1.0,
        structured_output_valid=False,
        memory_misuse=False,
        report_id=None,
        report_version=None,
        error=error,
    )


def _replay_metrics(
    results: list[EvalCaseResult],
    *,
    selected_count: int,
    skipped: list[dict[str, str]],
    limit: int,
    service: str | None,
    incident_ids: list[str],
) -> dict[str, Any]:
    metrics = eval_harness._suite_metrics(results)
    total = len(results) or 1
    metrics.update(
        {
            "suite_type": "historical_replay",
            "selected_count": selected_count,
            "skipped_count": len(skipped),
            "skipped": skipped[:50],
            "limit": limit,
            "service": service,
            "requested_incident_ids": incident_ids,
            "root_cause_consistency_rate": metrics["root_cause_top1_hit_rate"],
            "replay_drift_count": sum(1 for case in results if not case.root_cause_hit),
            "drifted_case_ids": [case.case_id for case in results if not case.root_cause_hit],
            "approval_interruption_rate": round(
                sum(1 for case in results if case.approval_interrupted) / total,
                4,
            ),
            "fixture_executor_forced": True,
            # Surface the safety boundary in the persisted metrics so API callers
            # can audit that replay did not write production incident artifacts.
            "write_scope": (
                "source_db_read_only; replay_writes_temp_db; "
                "api_persists_eval_run_metrics"
            ),
        }
    )
    return metrics
