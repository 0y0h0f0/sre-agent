"""Celery task — runs the LangGraph SRE diagnosis workflow asynchronously."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from apps.api.schemas.common import AgentRunStatus, IncidentStatus
from apps.worker.celery_app import celery_app
from packages.agent.llm import build_llm
from packages.agent.runner import AgentRunner
from packages.agent.schemas import AgentDeps
from packages.common import metrics as agent_metrics
from packages.common.errors import DependencyUnavailableError, NotFoundError
from packages.common.ids import new_id
from packages.common.settings import get_settings
from packages.db.models import AgentRunNode
from packages.db.repositories.agent_runs import TERMINAL_RUN_STATUSES, AgentRunRepository
from packages.db.repositories.email_logs import EmailLogRepository
from packages.db.repositories.incidents import IncidentRepository
from packages.db.repositories.runbooks import RunbookChunkRepository
from packages.db.repositories.tool_calls import ToolCallRepository
from packages.db.session import SessionLocal
from packages.memory.context_builder import ContextBuilder
from packages.memory.memory_store import MemoryStore
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
    build_executor_backend,
    build_k8s_backend,
    build_trace_backend,
)
from packages.tools.base import ToolResult
from packages.tools.cache import RequestLocalToolCache
from packages.tools.runbook_search import RunbookSearchTool
from packages.tools.unavailable import UnavailableTool


class TransientError(Exception):
    """Retryable worker dependency failure."""


def _email_notification_service(db: Session) -> Any:
    from apps.api.services.email_service import EmailNotificationService

    return EmailNotificationService(db, get_settings())


def enqueue_diagnosis_task(incident_id: str, agent_run_id: str) -> str:
    async_result = run_incident_diagnosis.delay(incident_id, agent_run_id)
    return str(async_result.id)


def enqueue_resume_task(agent_run_id: str, decision: str) -> str:
    """Enqueue a task to resume the graph after approval/rejection."""
    async_result = resume_incident_after_approval.delay(agent_run_id, decision)
    return str(async_result.id)


def enqueue_email_notification_task(notification_type: str, payload: dict[str, Any]) -> str:
    with SessionLocal() as db:
        queued = _email_notification_service(db).queue_event(
            notification_type, payload
        )
        email_log_id = str(queued["email_log_id"])

    try:
        async_result = send_email_notification.delay(email_log_id, notification_type, payload)
    except Exception as exc:
        with SessionLocal() as db:
            _email_notification_service(db).mark_enqueue_failed(email_log_id, str(exc))
        raise
    return str(async_result.id)


@celery_app.task(  # type: ignore[untyped-decorator]
    bind=True, autoretry_for=(TransientError,), retry_backoff=True, max_retries=2
)
def run_incident_diagnosis(self: Any, incident_id: str, agent_run_id: str) -> dict[str, Any]:
    # Celery doesn't natively retry on SoftTimeLimitExceeded, but a
    # timeout often resolves on retry (LLM latency spike, network blip).
    # The except-block below converts SoftTimeLimitExceeded → TransientError
    # so Celery's autoretry_for will pick it up on the re-raise.
    try:
        with SessionLocal() as db:
            return run_incident_diagnosis_logic(db, incident_id, agent_run_id)
    except TransientError:
        raise
    except Exception as exc:
        # Convert SoftTimeLimitExceeded to TransientError for autoretry
        from celery.exceptions import SoftTimeLimitExceeded
        if isinstance(exc, SoftTimeLimitExceeded):
            raise TransientError(str(exc)) from exc
        raise
    # NOTE: all paths above either return or raise; this line is unreachable
    # and kept as a comment to prevent future maintainers from accidentally
    # re-adding a double-execution path.
    # with SessionLocal() as db:
    #     return run_incident_diagnosis_logic(db, incident_id, agent_run_id)


def run_incident_diagnosis_logic(
    db: Session, incident_id: str, agent_run_id: str
) -> dict[str, Any]:
    incidents = IncidentRepository(db)
    runs = AgentRunRepository(db)

    incident = incidents.get_by_public_id(incident_id)
    if incident is None:
        raise NotFoundError("incident", incident_id)
    # Lock the run row so a redelivered task cannot claim it concurrently.
    run = runs.get_for_update(agent_run_id)
    if run is None:
        raise NotFoundError("agent_run", agent_run_id)
    if run.status in TERMINAL_RUN_STATUSES:
        db.rollback()
        return {"agent_run_id": agent_run_id, "status": run.status, "idempotent": True}
    # Already in flight (RUNNING) or paused for a human (WAITING_APPROVAL):
    # a duplicate delivery must not restart the graph or re-create approvals.
    # Check for orphaned runs (previous worker killed by SIGKILL) — if the run
    # has been RUNNING longer than the orphan timeout, assume the previous
    # worker is dead and re-execute.
    if run.status == AgentRunStatus.RUNNING.value:
        from packages.common.time import utc_now as _utc

        _orphan_timeout = get_settings().task_orphan_timeout_seconds
        if (
            run.started_at is None
            or (_utc() - run.started_at).total_seconds() < _orphan_timeout
        ):
            db.rollback()
            return {"agent_run_id": agent_run_id, "status": run.status, "idempotent": True}
        # Fall through: previous worker died, re-execute.
    elif run.status == AgentRunStatus.WAITING_APPROVAL.value:
        db.rollback()
        return {"agent_run_id": agent_run_id, "status": run.status, "idempotent": True}

    # Claim the run and commit immediately to release the row lock; a competing
    # worker then observes RUNNING above and short-circuits.
    runs.mark_running(run)
    incident.status = IncidentStatus.DIAGNOSING.value
    db.commit()

    agent_metrics.AgentMetricsCollector.inc_active_diagnoses()

    checkpointer: Any | None = None

    try:
        settings = get_settings()
        alert_payload = incidents.alert_payload(incident)
        deps = _build_deps(db, settings, agent_run_id, incident_id)

        # Wire PostgresSaver for checkpoint persistence
        checkpointer = _build_checkpointer(settings)

        runner = AgentRunner(deps, checkpointer=checkpointer)
        result = runner.run(incident_id, agent_run_id, alert_payload)

        if result["status"] == "waiting_approval":
            _sync_incident_diagnosis(incident, result.get("state", {}))
            _handle_waiting_approval(db, agent_run_id, result["state"])
            db.commit()
            _notify_diagnosis_complete(incident_id, agent_run_id, db=db)
            _notify_approval_requests(result["state"], db=db)
            return {
                "incident_id": incident_id,
                "agent_run_id": agent_run_id,
                "status": AgentRunStatus.WAITING_APPROVAL.value,
            }

        if result["status"] == "failed":
            error_msg = result.get("error", "unknown error")
            runs.mark_failed(agent_run_id, "GRAPH_FAILED", error_msg)
            db.commit()
            raise TransientError(error_msg)

        state_dict = _sanitize_state(result.get("state", {}))
        _sync_incident_diagnosis(incident, state_dict)

        # Populate token/cache columns on AgentRun (Phase 7.2)
        _populate_run_metrics(run, state_dict, deps.tool_cache)

        runs.mark_succeeded(run, state_dict)
        # Only mark mitigated if actions were actually executed
        if result.get("state", {}).get("execution_results"):
            incident.status = IncidentStatus.MITIGATED.value
        else:
            incident.status = IncidentStatus.RESOLVED.value
        db.commit()

        # Record NFA when resolved without actions
        if incident.status == IncidentStatus.RESOLVED.value:
            agent_metrics.AgentMetricsCollector.record_nfa(service=incident.service)

        _notify_diagnosis_complete(incident_id, agent_run_id, db=db)
        _notify_report_generated(state_dict, db=db)

        duration_s = _run_duration_seconds(run)
        agent_metrics.AgentMetricsCollector.record_diagnosis_completed(
            status="succeeded",
            duration_seconds=duration_s,
            model=run.model_name,
            prompt_tokens=run.total_prompt_tokens or 0,
            completion_tokens=run.total_completion_tokens or 0,
        )
        agent_metrics.AgentMetricsCollector.dec_active_diagnoses()
        return {
            "incident_id": incident_id,
            "agent_run_id": agent_run_id,
            "status": AgentRunStatus.SUCCEEDED.value,
        }

    except TransientError:
        agent_metrics.AgentMetricsCollector.dec_active_diagnoses()
        raise
    except Exception as exc:
        db.rollback()
        runs.mark_failed(agent_run_id, "DIAGNOSIS_FAILED", str(exc))
        db.commit()
        duration_s = _run_duration_seconds(run)
        agent_metrics.AgentMetricsCollector.record_diagnosis_completed(
            status="failed",
            duration_seconds=duration_s,
            model=run.model_name,
        )
        agent_metrics.AgentMetricsCollector.dec_active_diagnoses()
        raise
    finally:
        _close_checkpointer(checkpointer)


def _build_checkpointer(settings: Any) -> Any:
    """Create a PostgresSaver when a real database is configured.

    Returns ``None`` only when checkpointing is *intentionally* disabled
    (sqlite / in-memory / dev), where auto-approval is acceptable. For a
    configured real database, a build failure RAISES instead of returning
    ``None``: without a checkpointer the graph cannot interrupt for approval and
    would silently auto-approve every L2/L3 action. Failing closed here turns a
    checkpointer outage into a failed run rather than a bypassed approval gate.
    """
    db_url = settings.database_url
    if not db_url or "sqlite" in db_url or "memory" in db_url:
        return None

    try:
        from langgraph.checkpoint.postgres import PostgresSaver

        saver_context = PostgresSaver.from_conn_string(
            _postgres_saver_conn_string(db_url)
        )
        saver = saver_context.__enter__()
    except Exception as exc:
        raise DependencyUnavailableError(
            "checkpointer",
            "failed to initialize the approval checkpointer; refusing to run "
            "without the human-approval gate",
        ) from exc

    try:
        saver.setup()
    except Exception as exc:
        # Release the connection opened by __enter__ before failing closed.
        saver_context.__exit__(type(exc), exc, exc.__traceback__)
        raise DependencyUnavailableError(
            "checkpointer",
            "failed to initialize the approval checkpointer; refusing to run "
            "without the human-approval gate",
        ) from exc

    saver._codex_context_manager = saver_context  # type: ignore[attr-defined]
    return saver


def _postgres_saver_conn_string(db_url: str) -> str:
    """Return a psycopg-compatible connection string for PostgresSaver.

    Application database settings use SQLAlchemy URLs with driver suffixes,
    for example postgresql+psycopg://host/db. PostgresSaver hands the value
    directly to psycopg, which accepts postgresql://host/db but not the
    SQLAlchemy driver suffix.
    """
    if "://" not in db_url:
        return db_url
    scheme, rest = db_url.split("://", 1)
    if scheme.startswith(("postgresql+", "postgres+")):
        scheme = scheme.split("+", 1)[0]
    return f"{scheme}://{rest}"


def _close_checkpointer(checkpointer: Any | None) -> None:
    context = getattr(checkpointer, "_codex_context_manager", None)
    if context is not None:
        context.__exit__(None, None, None)


def _build_deps(db: Session, settings: Any, agent_run_id: str, incident_id: str) -> AgentDeps:
    """Build AgentDeps with effective config integration (M5 PR 5.5).

    - Production: uses EffectiveConfig.from_operator_sources() with full
      priority chain (env > override > profile > published > safe default).
    - Demo/local: uses EffectiveConfig.from_demo_sources() with settings defaults.
    - Missing backends get UnavailableTool (never None passed to tool constructors).
    """
    from packages.db.repositories.effective_configs import EffectiveConfigRepository
    from packages.discovery.config_merge import EffectiveConfig

    cache = RequestLocalToolCache()
    timeout = settings.tool_timeout_seconds

    # ------------------------------------------------------------------
    # Determine effective config source.
    # ------------------------------------------------------------------
    effective_config: Any = None
    config_version_id: str | None = None

    if settings.app_env == "production":
        # Production path: full priority chain.
        # Worker reads published config only — never proposals.
        ec_repo = EffectiveConfigRepository(db)
        published_version = ec_repo.get_latest_published()
        published_config = (
            published_version.config_snapshot
            if published_version and published_version.config_snapshot
            else None
        )
        if published_version is not None:
            config_version_id = published_version.version_id

        effective_config = EffectiveConfig.from_operator_sources(
            settings,
            published_config=published_config,
        )
    else:
        # Demo/local path: use settings defaults (backward compatible).
        effective_config = EffectiveConfig.from_demo_sources(settings)

    # ------------------------------------------------------------------
    # Build tools using effective config URLs; use UnavailableTool
    # when a backend URL is None to avoid crashing real tool constructors.
    # ------------------------------------------------------------------
    metrics_tool: Any = _build_or_unavailable(
        MetricsTool,
        effective_config.prometheus.url,
        "metrics",
        timeout_seconds=timeout,
        cache=cache,
        service_label=effective_config.metrics_service_label,
        step_seconds=settings.metrics_step_seconds,
        max_window_seconds=settings.metrics_max_window_seconds,
        max_shards=settings.metrics_max_shards,
    )

    logs_tool: Any = _build_or_unavailable(
        LogsTool,
        effective_config.loki.url,
        "logs",
        timeout_seconds=timeout,
        cache=cache,
        service_label=effective_config.logs_service_label,
    )

    # Trace tool with backend.
    trace_tool: Any
    if effective_config.jaeger.url:
        trace_tool = TraceTool(
            backend=build_trace_backend(settings),
            timeout_seconds=timeout,
            cache=cache,
        )
    else:
        trace_tool = UnavailableTool("trace", reason="Jaeger backend not configured")

    git_change_tool = GitChangeTool(
        backend=build_deployment_backend(settings), timeout_seconds=timeout, cache=cache
    )
    k8s_tool = K8sDiagnosticsTool(backend=build_k8s_backend(settings), timeout_seconds=timeout)
    db_diagnostics_tool = DbDiagnosticsTool(
        backend=build_db_diagnostics_backend(settings), timeout_seconds=timeout
    )
    executor_backend = build_executor_backend(settings)

    chunk_repo = RunbookChunkRepository(db)
    retriever = RunbookRetriever(
        chunk_repo,
        use_hybrid=settings.runbook_hybrid_search_enabled,
    )
    runbook_search_tool = RunbookSearchTool(
        retriever=retriever, timeout_seconds=timeout, cache=cache
    )

    memory_store = MemoryStore(db)
    context_builder = ContextBuilder()
    llm = build_llm(settings)
    tool_calls_repo = ToolCallRepository(db)

    def node_tracer(**kwargs: Any) -> None:
        started = kwargs.get("started_at")
        finished = kwargs.get("finished_at")
        duration_ms = (
            max(0, int((finished - started).total_seconds() * 1000)) if started and finished else 0
        )
        node = AgentRunNode(
            node_id=kwargs.get("node_id", new_id("nd_")),
            agent_run_id=kwargs.get("agent_run_id", agent_run_id),
            name=kwargs.get("name", "unknown"),
            status=kwargs.get("status", "unknown"),
            started_at=started,
            finished_at=finished,
            duration_ms=duration_ms,
            input_summary=(kwargs.get("input_summary") or "")[:500],
            output_summary=(kwargs.get("output_summary") or "")[:500],
            error_message=(kwargs.get("error_message") or "")[:500] or None,
        )
        db.add(node)
        db.flush()

        # Publish node update via Redis for WebSocket subscribers
        try:
            from apps.api.ws.publisher import publish_node_event

            publish_node_event(
                incident_id=incident_id,
                agent_run_id=agent_run_id,
                node_name=kwargs.get("name", "unknown"),
                status=kwargs.get("status", "unknown"),
                duration_ms=duration_ms,
                input_summary=(kwargs.get("input_summary") or "")[:200],
                output_summary=(kwargs.get("output_summary") or "")[:200],
            )
        except Exception:
            import logging
            logging.getLogger(__name__).warning(
                "failed to publish node event for run %s", agent_run_id, exc_info=True
            )

    def tool_call_recorder(**kwargs: Any) -> None:
        tool_calls_repo.create(
            agent_run_id=kwargs.get("agent_run_id", agent_run_id),
            node_name=kwargs.get("node_name", "unknown"),
            tool_name=kwargs.get("tool_name", "unknown"),
            query=kwargs.get("query", {}),
            result=kwargs.get(
                "result", ToolResult(status="degraded", data={}, summary="", duration_ms=0)
            ),
            input_summary=kwargs.get("input_summary", ""),
        )
        db.flush()

    return AgentDeps(
        db=db,
        settings=settings,
        tool_cache=cache,
        metrics_tool=metrics_tool,
        logs_tool=logs_tool,
        trace_tool=trace_tool,
        git_change_tool=git_change_tool,
        k8s_tool=k8s_tool,
        db_diagnostics_tool=db_diagnostics_tool,
        runbook_search_tool=runbook_search_tool,
        memory_store=memory_store,
        context_builder=context_builder,
        llm=llm,
        node_tracer=node_tracer,
        tool_call_recorder=tool_call_recorder,
        executor_backend=executor_backend,
        effective_config=effective_config,
        config_version_id=config_version_id,
    )


def _build_or_unavailable(
    tool_cls: Any,
    url: str | None,
    name: str,
    **kwargs: Any,
) -> Any:
    """Build a tool from ``tool_cls`` or return ``UnavailableTool`` if url is None.

    This prevents passing ``None`` to real tool constructors that call
    ``base_url.rstrip()`` and similar operations.
    """
    if url is not None:
        return tool_cls(base_url=url, **kwargs)
    return UnavailableTool(name, reason=f"{name} backend URL not configured")


def _sync_incident_diagnosis(incident: Any, state: dict[str, Any]) -> None:
    import logging
    _logger = logging.getLogger(__name__)

    # Surface accumulated node errors so operators can see them in logs.
    errors = state.get("errors")
    if isinstance(errors, list) and errors:
        _logger.warning(
            "diagnosis completed with %d node errors: %s",
            len(errors), [e.get("node", "?") for e in errors[-10:]],
        )

    root_cause = state.get("root_cause")
    summary = root_cause.get("summary") if isinstance(root_cause, dict) else None
    if not summary:
        report = state.get("incident_report")
        summary = report.get("root_cause") if isinstance(report, dict) else None
    if not summary:
        summary = state.get("diagnosis_rationale")
    if summary:
        incident.root_cause_summary = str(summary)


def _populate_run_metrics(
    run: Any, state: dict[str, Any], cache: RequestLocalToolCache
) -> None:
    """Populate AgentRun token/cache columns from graph execution state."""
    # Token usage from llm_calls recorded in the state
    llm_calls = state.get("llm_calls")
    if isinstance(llm_calls, list):
        total_prompt = 0
        total_completion = 0
        provider_hits = 0
        provider_misses = 0
        for call in llm_calls:
            if isinstance(call, dict):
                usage = call.get("usage") or {}
                total_prompt += int(usage.get("prompt_tokens", 0) or 0)
                total_completion += int(usage.get("completion_tokens", 0) or 0)
                if call.get("cache_hit"):
                    provider_hits += 1
                else:
                    provider_misses += 1
        run.total_prompt_tokens = total_prompt
        run.total_completion_tokens = total_completion
        run.provider_cache_hit_count = provider_hits
        run.provider_cache_miss_count = provider_misses

    # App-level tool cache stats
    run.app_cache_hit_count = cache.hit_count
    run.app_cache_miss_count = cache.miss_count


def _run_duration_seconds(run: Any) -> float:
    """Compute run duration in seconds from started_at to now or finished_at."""
    from packages.common.time import utc_now

    started = run.started_at
    finished = run.finished_at or utc_now()
    if started is None:
        return 0.0
    return max(0, (finished - started).total_seconds())  # type: ignore[no-any-return]


def _record_approval_metrics(
    db: Session, agent_run_id: str, decision: str
) -> None:
    """Record approval response time from the approval record."""
    from packages.db.repositories.approvals import ApprovalRepository

    approvals = ApprovalRepository(db).list_for_run(agent_run_id)
    for approval in approvals:
        if (
            approval.decided_at is not None
            and approval.requested_at is not None
        ):
            response_time = max(
                0,
                (approval.decided_at - approval.requested_at).total_seconds(),
            )
            agent_metrics.AgentMetricsCollector.record_approval_decision(
                decision=decision,
                response_time_seconds=response_time,
            )


def _handle_waiting_approval(db: Session, agent_run_id: str, state: dict[str, Any]) -> None:
    runs = AgentRunRepository(db)
    run = runs.get_by_public_id(agent_run_id)
    if run is None:
        return
    run.status = AgentRunStatus.WAITING_APPROVAL.value
    run.state = _sanitize_state(state)


def _sanitize_state(state: dict[str, Any]) -> dict[str, Any]:
    """Remove internal keys and convert datetimes to ISO strings."""
    from datetime import datetime as dt

    def _convert(obj: Any) -> Any:
        if isinstance(obj, dt):
            return obj.isoformat()
        if isinstance(obj, dict):
            return {k: _convert(v) for k, v in obj.items() if not k.startswith("_")}
        if isinstance(obj, list):
            return [_convert(v) for v in obj]
        return obj

    converted = _convert(state)
    return converted if isinstance(converted, dict) else {}


@celery_app.task(  # type: ignore[untyped-decorator]
    bind=True, autoretry_for=(TransientError,), retry_backoff=True, max_retries=2
)
def resume_incident_after_approval(self: Any, agent_run_id: str, decision: str) -> dict[str, Any]:
    """Resume a LangGraph workflow after approval/rejection decision."""
    with SessionLocal() as db:
        return _resume_incident_logic(db, agent_run_id, decision)


def _resume_incident_logic(db: Session, agent_run_id: str, decision: str) -> dict[str, Any]:
    if decision not in ("approved", "rejected"):
        raise ValueError(
            f"invalid decision '{decision}'; expected 'approved' or 'rejected'"
        )
    runs = AgentRunRepository(db)
    incidents = IncidentRepository(db)

    # Lock the run row so a redelivered resume cannot resume concurrently.
    run = runs.get_for_update(agent_run_id)
    if run is None:
        raise NotFoundError("agent_run", agent_run_id)
    if run.status != AgentRunStatus.WAITING_APPROVAL.value:
        return {"agent_run_id": agent_run_id, "status": run.status, "idempotent": True}

    # Claim the run (WAITING_APPROVAL -> RUNNING) and commit to release the lock;
    # a competing resume then sees RUNNING and short-circuits as idempotent.
    runs.mark_running(run)
    db.commit()

    checkpointer: Any | None = None

    try:
        settings = get_settings()
        deps = _build_deps(db, settings, agent_run_id, run.incident_id)
        checkpointer = _build_checkpointer(settings)

        runner = AgentRunner(deps, checkpointer=checkpointer)
        result = runner.resume(agent_run_id, decision)

        if result["status"] == "failed":
            error_msg = result.get("error", "unknown error")
            runs.mark_failed(agent_run_id, "RESUME_FAILED", error_msg)
            db.commit()
            raise TransientError(error_msg)

        # The graph may pause again (e.g. a rejection triggered a fresh
        # plan that needs a new approval round). Keep the run waiting.
        if result["status"] == "waiting_approval":
            incident = incidents.get_by_public_id(run.incident_id)
            if incident is not None:
                _sync_incident_diagnosis(incident, result.get("state", {}))
            _handle_waiting_approval(db, agent_run_id, result["state"])
            db.commit()
            _notify_diagnosis_complete(run.incident_id, agent_run_id, db=db)
            _notify_approval_requests(result["state"], db=db)
            return {
                "agent_run_id": agent_run_id,
                "status": AgentRunStatus.WAITING_APPROVAL.value,
                "decision": decision,
            }

        state_dict = _sanitize_state(result.get("state", {}))
        runs.mark_succeeded(run, state_dict)

        # Record approval response time from the approval record (Phase 7.2)
        try:
            _record_approval_metrics(db, agent_run_id, decision)
        except Exception:
            import logging
            logging.getLogger(__name__).warning(
                "failed to record approval metrics for run %s", agent_run_id, exc_info=True
            )

        # Finalize the incident: mitigated only if actions actually executed,
        # otherwise resolved.
        incident = incidents.get_by_public_id(run.incident_id)
        if incident is not None:
            _sync_incident_diagnosis(incident, state_dict)
            if result.get("state", {}).get("execution_results"):
                incident.status = IncidentStatus.MITIGATED.value
            else:
                incident.status = IncidentStatus.RESOLVED.value
        db.commit()
        _notify_diagnosis_complete(run.incident_id, agent_run_id, db=db)
        _notify_report_generated(state_dict, db=db)
        return {
            "agent_run_id": agent_run_id,
            "status": AgentRunStatus.SUCCEEDED.value,
            "decision": decision,
        }

    except TransientError:
        raise
    except Exception as exc:
        db.rollback()
        runs.mark_failed(agent_run_id, "RESUME_FAILED", str(exc))
        db.commit()
        raise
    finally:
        _close_checkpointer(checkpointer)


def _notify_diagnosis_complete(
    incident_id: str, agent_run_id: str, *, db: Session | None = None
) -> None:
    if _email_event_exists(
        db,
        notification_type="diagnosis_complete",
        related_agent_run_id=agent_run_id,
    ):
        return
    _enqueue_notification_event(
        "diagnosis_complete",
        {"incident_id": incident_id, "agent_run_id": agent_run_id},
    )


def _notify_approval_requests(state: dict[str, Any], *, db: Session | None = None) -> None:
    approval_status = state.get("approval_status")
    if not isinstance(approval_status, dict):
        import logging
        logging.getLogger(__name__).warning(
            "approval_status missing or invalid in state — no approval emails will be sent. "
            "state keys: %s", list(state.keys())[:10]
        )
        return
    approval_ids = approval_status.get("approval_ids")
    if not isinstance(approval_ids, list):
        return
    import logging
    logger = logging.getLogger(__name__)
    logger.info("Enqueuing approval request emails for %d approval(s)", len(approval_ids))
    for approval_id in approval_ids:
        approval_id_str = str(approval_id)
        if _email_event_exists(
            db,
            notification_type="approval_request",
            related_approval_id=approval_id_str,
        ):
            continue
        _enqueue_notification_event("approval_request", {"approval_id": approval_id_str})


def _notify_report_generated(state: dict[str, Any], *, db: Session | None = None) -> None:
    report = state.get("incident_report")
    if not isinstance(report, dict):
        return
    report_id = report.get("report_id")
    if not report_id:
        return
    report_id_str = str(report_id)
    if _email_event_exists(
        db, notification_type="incident_report", related_report_id=report_id_str
    ):
        return
    _enqueue_notification_event("incident_report", {"report_id": report_id_str})


def _email_event_exists(db: Session | None, **criteria: Any) -> bool:
    if db is None:
        return False
    return EmailLogRepository(db).exists_for_event(**criteria)


def _enqueue_notification_event(notification_type: str, payload: dict[str, Any]) -> None:
    try:
        enqueue_email_notification_task(notification_type, payload)
    except Exception:
        import logging
        logging.getLogger(__name__).error(
            "failed to enqueue %s notification: %s",
            notification_type, str(payload)[:200], exc_info=True,
        )


@celery_app.task(bind=True, max_retries=3)  # type: ignore[untyped-decorator]
def send_email_notification(
    self: Any, email_log_id: str, notification_type: str, payload: dict[str, Any]
) -> dict[str, Any]:
    attempt = int(getattr(self.request, "retries", 0)) + 1
    with SessionLocal() as db:
        result = _email_notification_service(db).send_queued_event(
            email_log_id, notification_type, payload, attempt=attempt
        )

    if result.get("retryable") and int(getattr(self.request, "retries", 0)) < 3:
        raise self.retry(countdown=5, exc=TransientError(str(result.get("error", "email failed"))))
    return result  # type: ignore[no-any-return]


@celery_app.task(bind=True, max_retries=3)  # type: ignore[untyped-decorator]
def send_daily_incident_summary(
    self: Any, summary_date: str | None = None, email_log_id: str | None = None
) -> dict[str, Any]:
    payload = {"date": summary_date} if summary_date else {}
    attempt = int(getattr(self.request, "retries", 0)) + 1
    with SessionLocal() as db:
        service = _email_notification_service(db)
        if email_log_id is None:
            queued = service.queue_event("daily_summary", payload)
            email_log_id = str(queued["email_log_id"])
        result = service.send_queued_event(email_log_id, "daily_summary", payload, attempt=attempt)

    if result.get("retryable") and int(getattr(self.request, "retries", 0)) < 3:
        raise self.retry(
            args=(summary_date, email_log_id),
            countdown=5,
            exc=TransientError(str(result.get("error", "email failed"))),
        )
    return result  # type: ignore[no-any-return]


def enqueue_discovery_rerun_task(discovery_run_id: str, triggered_by: str | None = None) -> str:
    """Enqueue a Celery task to run discovery asynchronously."""
    async_result = run_discovery_rerun.delay(discovery_run_id, triggered_by=triggered_by)
    return str(async_result.id)


@celery_app.task(  # type: ignore[untyped-decorator]
    bind=True,
    autoretry_for=(TransientError,),
    retry_backoff=True,
    max_retries=1,
)
def run_discovery_rerun(
    self: Any,
    discovery_run_id: str,
    triggered_by: str | None = None,
) -> dict[str, Any]:
    """Run a discovery scan asynchronously.

    Builds DiscoveryRunner from current settings, runs all discovery
    components, persists results via DiscoveryStore, and generates
    a DiscoveryProposal for config changes.

    Redis lock is acquired at the API layer (before enqueue) to
    prevent concurrent discovery runs.
    """

    from packages.db.repositories.audit_logs import AuditLogRepository
    from packages.discovery.store import DiscoveryStore

    settings = get_settings()

    try:
        with SessionLocal() as db:
            store = DiscoveryStore(db)
            audit_repo = AuditLogRepository(db)
            run = store.get_run(discovery_run_id)

            if run is None:
                raise NotFoundError("discovery_run", discovery_run_id)

            # Build runner from settings (will be refactored in PR 5.5).
            runner = _build_discovery_runner(settings)

            # Execute discovery.
            result = runner.run(run_id=discovery_run_id)

            # Persist result.
            store.finish_run(run, result, status=result.status)

            # Generate proposal if there are changes.
            if result.backend_endpoints or result.metric_mappings:
                store.create_proposal(
                    discovery_run_id=discovery_run_id,
                    config_diff=_result_to_config_diff(result),
                    confidence=0.8 if result.status == "succeeded" else 0.5,
                    status="pending_review",
                )

            audit_repo.create_discovery_audit(
                action="discovery.rerun_complete",
                resource_type="discovery_run",
                resource_id=discovery_run_id,
                actor=triggered_by or "system",
                details={
                    "status": result.status,
                    "total_services": result.total_services_discovered,
                    "total_metrics": result.total_metrics_scanned,
                    "duration_seconds": result.duration_seconds,
                    "warnings": result.warnings,
                },
            )

            db.commit()
            return {
                "discovery_run_id": discovery_run_id,
                "status": result.status,
            }
    except Exception as exc:
        # Mark the run as failed.
        try:
            with SessionLocal() as db:
                store = DiscoveryStore(db)
                run = store.get_run(discovery_run_id)
                if run:
                    empty_result = _empty_discovery_result()
                    store.finish_run(
                        run,
                        empty_result,
                        status="failed",
                        error_message=str(exc),
                    )
                    db.commit()
        except Exception:
            pass
        raise


@celery_app.task(  # type: ignore[untyped-decorator]
    bind=True,
    max_retries=0,
)
def auto_discovery_rerun(self: Any) -> dict[str, Any]:
    """Periodic auto-discovery — runs silently without requiring manual trigger.

    Scheduled by Celery Beat (every 30 min by default).
    Skips if discovery is disabled or K8s backend is not live.
    Returns immediately if another discovery run is already in progress.
    """
    import redis as redis_lib

    from packages.common.redis_lock import RedisLock
    from packages.db.repositories.audit_logs import AuditLogRepository
    from packages.discovery.store import DiscoveryStore

    settings = get_settings()

    if not settings.discovery_enabled:
        return {"status": "skipped", "reason": "discovery_disabled"}
    if settings.k8s_backend != "live":
        return {"status": "skipped", "reason": "k8s_backend_not_live"}

    # Lightweight lock to prevent concurrent auto-discovery runs.
    r = redis_lib.Redis.from_url(settings.redis_url)
    lock_key = "lock:discovery:auto"
    lock = RedisLock(r, lock_key, ttl=60)
    if not lock.acquire():
        return {"status": "skipped", "reason": "discovery_lock_held"}

    try:
        with SessionLocal() as db:
            store = DiscoveryStore(db)
            run = store.create_run(
                source="auto_periodic",
                trigger_type="periodic",
            )
            discovery_run_id = run.discovery_run_id
            db.commit()

        runner = _build_discovery_runner(settings)
        result = runner.run(run_id=discovery_run_id)

        with SessionLocal() as db:
            store = DiscoveryStore(db)
            run = store.get_run(discovery_run_id)
            if run:
                store.finish_run(run, result, status=result.status)
            audit_repo = AuditLogRepository(db)
            audit_repo.create_discovery_audit(
                action="discovery.auto_complete",
                resource_type="discovery_run",
                resource_id=discovery_run_id,
                actor="auto_discovery",
                details={
                    "status": result.status,
                    "services_discovered": result.total_services_discovered,
                    "warnings": result.warnings,
                },
            )
            db.commit()

        return {
            "discovery_run_id": discovery_run_id,
            "status": result.status,
            "services_discovered": result.total_services_discovered,
        }
    except Exception as exc:
        return {"status": "failed", "reason": str(exc)}
    finally:
        try:
            lock.release()
        except Exception:
            pass


def _build_discovery_runner(settings: Any) -> Any:
    """Build a DiscoveryRunner from current settings.

    Uses settings to construct backend clients. Will be refactored in
    PR 5.5 to use EffectiveConfig.from_operator_sources().
    """
    from packages.discovery.backend_endpoints import BackendEndpointDetector
    from packages.discovery.k8s_discovery import K8sDiscovery
    from packages.discovery.loki_discovery import LokiClient
    from packages.discovery.prom_discovery import PrometheusClient
    from packages.discovery.runner import DiscoveryRunner

    prom_client = None
    loki_client = None
    k8s = None
    jaeger_client = None

    # Build Prometheus client if URL is configured.
    if settings.prometheus_url:
        prom_client = PrometheusClient(settings.prometheus_url)

    # Build Loki client if URL is configured.
    if settings.loki_url:
        loki_client = LokiClient(settings.loki_url)

    # Build K8s discovery if enabled.
    if settings.discovery_enabled and settings.k8s_backend == "live":
        try:
            # K8sDiscovery internally parses settings.k8s_namespace as a
            # comma-separated allowlist when no explicit allowlist is given.
            k8s = K8sDiscovery()
        except Exception:
            pass

    # Build Jaeger client if URL is configured.
    if settings.jaeger_url:
        try:
            from packages.discovery.jaeger_discovery import JaegerDiscoveryClient
            jaeger_client = JaegerDiscoveryClient(settings.jaeger_url)
        except Exception:
            pass

    # Build backend endpoint detector; DiscoveryRunner passes the K8s result
    # from the same run into detect().
    backend_detector = None
    if k8s is not None:
        try:
            backend_detector = BackendEndpointDetector()
        except Exception:
            pass

    return DiscoveryRunner(
        k8s=k8s,
        prom_client=prom_client,
        loki_client=loki_client,
        jaeger_client=jaeger_client,
        backend_detector=backend_detector,
        metrics_service_label=settings.metrics_service_label,
        logs_service_label=settings.logs_service_label,
    )


def _result_to_config_diff(result: Any) -> dict[str, Any]:
    """Convert a DiscoveryResult to a config_diff for proposal creation."""
    diff: dict[str, Any] = {}
    if hasattr(result, "backend_endpoints") and result.backend_endpoints:
        diff["backend_endpoints"] = [
            {
                "backend_type": (
                    ep.backend_type if hasattr(ep, "backend_type")
                    else ep.get("backend_type")
                ),
                "url": ep.url if hasattr(ep, "url") else ep.get("url"),
                "status": (
                    ep.status if hasattr(ep, "status")
                    else ep.get("status")
                ),
            }
            for ep in result.backend_endpoints
        ]
    if hasattr(result, "metric_mappings") and result.metric_mappings:
        diff["metric_mappings"] = [
            {
                "semantic_type": (
                    m.semantic_type if hasattr(m, "semantic_type")
                    else m.get("semantic_type")
                ),
                "metric_name": (
                    m.metric_name if hasattr(m, "metric_name")
                    else m.get("metric_name")
                ),
                "status": (
                    m.status if hasattr(m, "status")
                    else m.get("status")
                ),
            }
            for m in result.metric_mappings
        ]
    return diff


def _empty_discovery_result() -> Any:
    """Return an empty DiscoveryResult for failure cases."""
    from packages.discovery.models import DiscoveryResult
    return DiscoveryResult(status="failed")


@celery_app.task(bind=True)  # type: ignore[untyped-decorator]
def auto_approve_stale_approvals(self: Any) -> dict[str, Any]:
    """Auto-approve L2 (or lower) approvals that have been waiting beyond the threshold.

    Configured via ``APPROVAL_AUTO_APPROVE_MINUTES`` (0 = disabled) and
    ``APPROVAL_AUTO_APPROVE_MAX_RISK`` (default "L2"). L3+ are never
    auto-approved.
    """
    settings = get_settings()
    threshold_minutes = settings.approval_auto_approve_minutes
    if threshold_minutes <= 0:
        return {"status": "disabled", "threshold_minutes": 0}

    max_risk = settings.approval_auto_approve_max_risk
    if max_risk not in ("L0", "L1", "L2"):
        return {"status": "skipped", "reason": f"max_risk={max_risk} exceeds L2 cap"}

    risk_levels = {"L0": 0, "L1": 1, "L2": 2}
    max_risk_value = risk_levels.get(max_risk, 2)

    with SessionLocal() as db:
        from datetime import timedelta

        from sqlalchemy import select

        from apps.api.schemas.common import ActionStatus, ApprovalStatus
        from packages.common.time import utc_now
        from packages.db.models import Action, Approval
        from packages.db.repositories.actions import ActionRepository
        from packages.db.repositories.approvals import ApprovalRepository
        from packages.db.repositories.audit_logs import AuditLogRepository

        approvals_repo = ApprovalRepository(db)
        actions_repo = ActionRepository(db)
        audit_repo = AuditLogRepository(db)

        cutoff = utc_now() - timedelta(minutes=threshold_minutes)

        stmt = (
            select(Approval, Action)
            .join(Action, Approval.action_id == Action.action_id)
            .where(
                Approval.status == "waiting",
                Approval.requested_at < cutoff,
            )
        )
        stale_rows = list(db.execute(stmt).all())

        count = 0
        for approval, action in stale_rows:
            # Only auto-approve L0, L1, L2
            action_risk = risk_levels.get(action.risk_level, 99)
            if action_risk > max_risk_value:
                continue

            approvals_repo.update_decision(
                approval.approval_id,
                status=ApprovalStatus.APPROVED.value,
                approver="system-auto",
                comment=f"auto-approved after {threshold_minutes}min",
            )
            actions_repo.update_status(action.action_id, ActionStatus.APPROVED.value)
            audit_repo.create(
                incident_id=approval.incident_id,
                actor="system-auto",
                action="approve",
                resource_type="approval",
                resource_id=approval.approval_id,
                details={
                    "reason": "auto_approved",
                    "threshold_minutes": threshold_minutes,
                    "action_id": action.action_id,
                    "risk_level": action.risk_level,
                },
            )
            count += 1

        db.commit()

        # Resume runs where all approvals are now decided
        if count > 0:
            run_ids = list({a.agent_run_id for a, _ in stale_rows if a.status == "approved"})
            for run_id in run_ids:
                if not approvals_repo.has_waiting_for_run(run_id):
                    enqueue_resume_task(run_id, "approved")

        return {
            "status": "completed",
            "auto_approved": count,
            "threshold_minutes": threshold_minutes,
        }


# ---------------------------------------------------------------------------
# PR 4.7: Poll Alertmanager Task
# ---------------------------------------------------------------------------


def _build_filter_hash(filters: Any) -> str:
    """Build a stable, deterministic filter hash for poll scope dedup.

    The hash is derived from the effective poll filters (receiver, namespace,
    service allowlist, extra matchers) so that different poll scopes use
    different lock keys and cursor namespaces.
    """
    import hashlib
    import json

    canonical: dict[str, Any] = {}
    if filters.receiver:
        canonical["receiver"] = filters.receiver
    if filters.namespace_allowlist:
        canonical["namespace_allowlist"] = sorted(filters.namespace_allowlist)
    if filters.service_allowlist:
        canonical["service_allowlist"] = sorted(filters.service_allowlist)
    if filters.extra_matchers:
        canonical["extra_matchers"] = sorted(filters.extra_matchers)
    raw = json.dumps(canonical, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _get_poll_filters(settings: Any) -> Any:
    """Build AlertPollFilters from settings."""
    from packages.discovery.matcher_parser import AlertPollFilters

    receiver = settings.alert_poll_receiver_filter.strip() or None
    namespace_allowlist = [
        ns.strip()
        for ns in settings.alert_poll_namespace_allowlist.split(",")
        if ns.strip()
    ]
    service_allowlist = [
        svc.strip()
        for svc in settings.alert_poll_service_allowlist.split(",")
        if svc.strip()
    ]
    extra_matchers = [
        m.strip()
        for m in settings.alert_poll_filter_matchers.split(",")
        if m.strip()
    ]

    return AlertPollFilters(
        receiver=receiver,
        namespace_allowlist=namespace_allowlist,
        service_allowlist=service_allowlist,
        extra_matchers=extra_matchers,
    )


@celery_app.task(  # type: ignore[untyped-decorator]
    bind=True,
    autoretry_for=(TransientError,),
    retry_backoff=True,
    max_retries=1,
)
def poll_alertmanager(self: Any) -> dict[str, Any]:
    """Poll Alertmanager for alerts and create incidents.

    Uses Redis lock per effective filter hash to prevent concurrent polls.
    Produces the same fingerprint as the webhook path for deduplication.
    Includes conservative resolved inference via PR 4.6 logic.
    """
    import redis as redis_lib

    from packages.common.redis_lock import RedisLock
    from packages.discovery.matcher_parser import has_valid_scope

    settings = get_settings()

    # Quick skip if poll is not enabled.
    if settings.alert_source not in ("poll", "both"):
        return {"status": "skipped", "reason": "alert_source is not poll/both"}

    # Build filters and validate scope.
    filters = _get_poll_filters(settings)
    if not has_valid_scope(filters):
        return {"status": "skipped", "reason": "no valid poll scope configured"}

    filter_hash = _build_filter_hash(filters)

    # Acquire Redis lock per filter hash.
    try:
        r = redis_lib.Redis.from_url(settings.redis_url)
    except Exception:
        r = None

    if r is not None:
        lock_key = f"lock:poll:alertmanager:{filter_hash}"
        lock = RedisLock(r, lock_key, ttl=settings.alert_poll_lock_ttl_seconds)
        if not lock.acquire():
            return {
                "status": "locked",
                "filter_hash": filter_hash,
                "message": "Another poll instance holds the lock for this scope",
            }

    try:
        return _poll_alertmanager_logic(
            db_factory=SessionLocal,
            settings=settings,
            filters=filters,
            filter_hash=filter_hash,
        )
    except Exception:
        raise
    finally:
        if r is not None:
            try:
                lock.release()
            except Exception:
                pass


def _poll_alertmanager_logic(
    *,
    db_factory: Any,
    settings: Any,
    filters: Any,
    filter_hash: str,
) -> dict[str, Any]:
    """Core poll logic — fetch alerts, dedup, create incidents, infer resolved."""
    from packages.db.repositories.audit_logs import AuditLogRepository
    from packages.db.repositories.effective_configs import EffectiveConfigRepository
    from packages.db.repositories.poll_cursor import PollCursorRepository
    from packages.discovery.alertmanager_client import AlertmanagerClient
    from packages.discovery.config_merge import EffectiveConfig
    from packages.discovery.resolved_inference import (
        infer_resolved_from_missing_fingerprints,
    )

    with db_factory() as db:
        # Build EffectiveConfig to get Alertmanager URL.
        ec_repo = EffectiveConfigRepository(db)
        published_version = ec_repo.get_latest_published()
        published_config = (
            published_version.config_snapshot
            if published_version and published_version.config_snapshot
            else None
        )
        effective_config = EffectiveConfig.from_operator_sources(
            settings,
            published_config=published_config,
        )

        am_url = effective_config.alertmanager.url
        if not am_url:
            return {"status": "degraded", "reason": "No Alertmanager URL configured"}

        # Build Alertmanager client.
        client = AlertmanagerClient(am_url, timeout=settings.alert_poll_timeout_seconds)

        # Build server-side matchers from filters.
        from packages.discovery.matcher_parser import (
            _allowlist_to_server_matchers,
            can_map_to_server_side,
        )

        matchers: list[str] = []
        if can_map_to_server_side(filters):
            matchers = _allowlist_to_server_matchers(
                filters.namespace_allowlist,
                filters.service_allowlist,
                service_label=effective_config.metrics_service_label,
            )
            matchers.extend(filters.extra_matchers)

        # Fetch alerts (with truncation flag).
        results_truncated = False
        try:
            raw_alerts = client.list_alerts(
                filter_matchers=matchers if matchers else None,
                receiver=filters.receiver,
            )
            if len(raw_alerts) > settings.alert_poll_max_alerts_per_round:
                raw_alerts = raw_alerts[: settings.alert_poll_max_alerts_per_round]
                results_truncated = True
        except Exception as exc:
            _audit_poll(db, filter_hash, "failed", str(exc))
            return {"status": "failed", "reason": str(exc)[:200]}

        # Process each alert.
        from apps.api.schemas.alerts import _from_alertmanager_single_alert
        from apps.api.services.alert_service import AlertService

        cursor_repo = PollCursorRepository(db)
        audit_repo = AuditLogRepository(db)

        new_incidents = 0
        seen_fingerprints: set[str] = set()
        incidents_per_service: dict[str, int] = {}

        for alert in raw_alerts:
            if new_incidents >= settings.alert_poll_max_new_incidents_per_round:
                break

            parsed = _from_alertmanager_single_alert(alert)
            fingerprint = parsed["fingerprint"]

            # Dedup via poll cursor.
            if cursor_repo.already_seen_active(fingerprint, filter_hash):
                seen_fingerprints.add(fingerprint)
                continue

            # Rate-limit per service.
            svc = parsed["service"]
            if (
                incidents_per_service.get(svc, 0)
                >= settings.alert_poll_max_incidents_per_service_per_minute
            ):
                continue

            # Create incident via AlertService.
            try:
                from apps.api.schemas.alerts import AlertCreateRequest

                req = AlertCreateRequest(
                    source="alertmanager",
                    fingerprint=fingerprint,
                    service=svc,
                    severity=parsed["severity"],
                    alert_name=parsed["alert_name"],
                    starts_at=parsed["starts_at"],
                    ends_at=parsed["ends_at"],
                    labels=parsed["labels"],
                    annotations=parsed["annotations"],
                    raw_payload=alert,
                )

                alert_svc = AlertService(
                    db, settings, enqueue_diagnosis=enqueue_diagnosis_task
                )
                resp = alert_svc.create_alert(req)

                cursor_repo.mark_seen(
                    fingerprint=fingerprint,
                    incident_id=resp.incident_id,
                    filter_hash=filter_hash,
                )
                seen_fingerprints.add(fingerprint)
                new_incidents += 1
                incidents_per_service[svc] = incidents_per_service.get(svc, 0) + 1

            except Exception as exc:
                # Log and continue — single alert failure must not block the
                # entire poll round.
                import logging
                logging.getLogger(__name__).warning(
                    "poll_alertmanager: failed to create incident for "
                    "fingerprint=%s: %s", fingerprint, exc
                )
                continue

        # Run resolved inference on previously-seen fingerprints (PR 4.6).
        resolved_count = 0
        try:
            active_hashes = [filter_hash]  # single-poll use case
            for fp in cursor_repo.get_active_fingerprints(filter_hash):
                if fp in seen_fingerprints:
                    continue  # Still active.
                cursor_repo.mark_missing(fp, filter_hash)
                decision = infer_resolved_from_missing_fingerprints(
                    fingerprint=fp,
                    all_active_filter_hashes=active_hashes,
                    cursor_repo=cursor_repo,
                    results_truncated=results_truncated,
                    grace_rounds=max(
                        1,
                        settings.alert_poll_resolved_grace_period_seconds
                        // max(1, settings.alert_poll_interval_seconds),
                    ),
                    resolved_rounds=settings.alert_poll_resolved_missing_rounds,
                    poll_interval_seconds=settings.alert_poll_interval_seconds,
                )
                if decision.is_resolved:
                    from apps.api.schemas.common import IncidentStatus
                    from packages.db.repositories.incidents import IncidentRepository

                    incident_repo = IncidentRepository(db)
                    incident = incident_repo.get_open_by_fingerprint(fp)
                    if incident is not None and incident.status not in (
                        IncidentStatus.RESOLVED.value,
                        IncidentStatus.MITIGATED.value,
                    ):
                        incident.status = IncidentStatus.RESOLVED.value
                        audit_repo.create_config_audit(
                            action="incident.resolved_inferred",
                            resource_type="incident",
                            resource_id=incident.incident_id,
                            actor="poll_alertmanager",
                            details={
                                "fingerprint": fp,
                                "filter_hash": filter_hash,
                                "reason": decision.reason,
                                "evidence": decision.evidence,
                            },
                        )
                        resolved_count += 1
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning(
                "poll_alertmanager: resolved inference failed: %s", exc
            )

        db.commit()

        _audit_poll(
            db,
            filter_hash,
            "completed",
            extra={
                "new_incidents": new_incidents,
                "resolved_incidents": resolved_count,
                "total_alerts": len(raw_alerts),
                "truncated": results_truncated,
                "seen_count": len(seen_fingerprints),
            },
        )

        return {
            "status": "completed",
            "filter_hash": filter_hash,
            "new_incidents": new_incidents,
            "resolved_incidents": resolved_count,
            "total_alerts_scanned": len(raw_alerts),
            "truncated": results_truncated,
        }


def _audit_poll(
    db: Any,
    filter_hash: str,
    status: str,
    error: str | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """Write a poll audit log entry."""
    from packages.common.ids import new_id
    from packages.db.models import AuditLog

    details: dict[str, Any] = {"filter_hash": filter_hash, "status": status}
    if error:
        details["error"] = error
    if extra:
        details.update(extra)

    entry = AuditLog(
        audit_id=new_id("aud_"),
        actor="poll_alertmanager",
        action="alertmanager.poll",
        resource_type="alert_poll",
        resource_id=filter_hash or "default",
        details=details,
        source="beat",
    )
    db.add(entry)
    db.flush()
