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
    cache = RequestLocalToolCache()
    timeout = settings.tool_timeout_seconds

    metrics_tool = MetricsTool(
        base_url=settings.prometheus_url,
        timeout_seconds=timeout,
        cache=cache,
        service_label=settings.metrics_service_label,
        step_seconds=settings.metrics_step_seconds,
        max_window_seconds=settings.metrics_max_window_seconds,
        max_shards=settings.metrics_max_shards,
    )
    logs_tool = LogsTool(
        base_url=settings.loki_url,
        timeout_seconds=timeout,
        cache=cache,
        service_label=settings.logs_service_label,
    )
    # Phase 2.1: trace/deployment data sources are now pluggable backends
    # (default fixture). Phase 2.2/2.3 add read-only K8s and DB diagnosis tools.
    trace_tool = TraceTool(
        backend=build_trace_backend(settings), timeout_seconds=timeout, cache=cache
    )
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
    )


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
