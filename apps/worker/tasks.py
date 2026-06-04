"""Celery task — runs the LangGraph SRE diagnosis workflow asynchronously."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from apps.api.schemas.common import AgentRunStatus, IncidentStatus
from apps.api.services.email_service import EmailNotificationService
from apps.worker.celery_app import celery_app
from packages.agent.llm import build_llm
from packages.agent.runner import AgentRunner
from packages.agent.schemas import AgentDeps
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
    build_k8s_backend,
    build_trace_backend,
)
from packages.tools.base import ToolResult
from packages.tools.cache import RequestLocalToolCache
from packages.tools.runbook_search import RunbookSearchTool


class TransientError(Exception):
    """Retryable worker dependency failure."""


def enqueue_diagnosis_task(incident_id: str, agent_run_id: str) -> str:
    async_result = run_incident_diagnosis.delay(incident_id, agent_run_id)
    return str(async_result.id)


def enqueue_resume_task(agent_run_id: str, decision: str) -> str:
    """Enqueue a task to resume the graph after approval/rejection."""
    async_result = resume_incident_after_approval.delay(agent_run_id, decision)
    return str(async_result.id)


def enqueue_email_notification_task(notification_type: str, payload: dict[str, Any]) -> str:
    with SessionLocal() as db:
        queued = EmailNotificationService(db, get_settings()).queue_event(
            notification_type, payload
        )
        email_log_id = str(queued["email_log_id"])

    try:
        async_result = send_email_notification.delay(email_log_id, notification_type, payload)
    except Exception as exc:
        with SessionLocal() as db:
            EmailNotificationService(db, get_settings()).mark_enqueue_failed(email_log_id, str(exc))
        raise
    return str(async_result.id)


@celery_app.task(  # type: ignore[untyped-decorator]
    bind=True, autoretry_for=(TransientError,), retry_backoff=True, max_retries=2
)
def run_incident_diagnosis(self: Any, incident_id: str, agent_run_id: str) -> dict[str, Any]:
    with SessionLocal() as db:
        return run_incident_diagnosis_logic(db, incident_id, agent_run_id)


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
        return {"agent_run_id": agent_run_id, "status": run.status, "idempotent": True}
    # Already in flight (RUNNING) or paused for a human (WAITING_APPROVAL):
    # a duplicate delivery must not restart the graph or re-create approvals.
    if run.status in (AgentRunStatus.RUNNING.value, AgentRunStatus.WAITING_APPROVAL.value):
        return {"agent_run_id": agent_run_id, "status": run.status, "idempotent": True}

    # Claim the run and commit immediately to release the row lock; a competing
    # worker then observes RUNNING above and short-circuits.
    runs.mark_running(run)
    incident.status = IncidentStatus.DIAGNOSING.value
    db.commit()

    checkpointer: Any | None = None

    try:
        settings = get_settings()
        alert_payload = incidents.alert_payload(incident)
        deps = _build_deps(db, settings, agent_run_id)

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
        runs.mark_succeeded(run, state_dict)
        # Only mark mitigated if actions were actually executed
        if result.get("state", {}).get("execution_results"):
            incident.status = IncidentStatus.MITIGATED.value
        else:
            incident.status = IncidentStatus.RESOLVED.value
        db.commit()
        _notify_diagnosis_complete(incident_id, agent_run_id, db=db)
        _notify_report_generated(state_dict, db=db)
        return {
            "incident_id": incident_id,
            "agent_run_id": agent_run_id,
            "status": AgentRunStatus.SUCCEEDED.value,
        }

    except TransientError:
        raise
    except Exception as exc:
        db.rollback()
        runs.mark_failed(agent_run_id, "DIAGNOSIS_FAILED", str(exc))
        db.commit()
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

        saver_context = PostgresSaver.from_conn_string(db_url)
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


def _close_checkpointer(checkpointer: Any | None) -> None:
    context = getattr(checkpointer, "_codex_context_manager", None)
    if context is not None:
        context.__exit__(None, None, None)


def _build_deps(db: Session, settings: Any, agent_run_id: str) -> AgentDeps:
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

    chunk_repo = RunbookChunkRepository(db)
    retriever = RunbookRetriever(chunk_repo)
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
    )


def _sync_incident_diagnosis(incident: Any, state: dict[str, Any]) -> None:
    root_cause = state.get("root_cause")
    summary = root_cause.get("summary") if isinstance(root_cause, dict) else None
    if not summary:
        report = state.get("incident_report")
        summary = report.get("root_cause") if isinstance(report, dict) else None
    if not summary:
        summary = state.get("diagnosis_rationale")
    if summary:
        incident.root_cause_summary = str(summary)


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
        deps = _build_deps(db, settings, agent_run_id)
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
        # Finalize the incident: mitigated only if actions actually executed,
        # otherwise resolved. Without this the incident stayed in DIAGNOSING
        # forever and kept deduplicating future alerts.
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
        return
    approval_ids = approval_status.get("approval_ids")
    if not isinstance(approval_ids, list):
        return
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
        return


@celery_app.task(bind=True, max_retries=3)  # type: ignore[untyped-decorator]
def send_email_notification(
    self: Any, email_log_id: str, notification_type: str, payload: dict[str, Any]
) -> dict[str, Any]:
    attempt = int(getattr(self.request, "retries", 0)) + 1
    with SessionLocal() as db:
        result = EmailNotificationService(db, get_settings()).send_queued_event(
            email_log_id, notification_type, payload, attempt=attempt
        )

    if result.get("retryable") and int(getattr(self.request, "retries", 0)) < 3:
        raise self.retry(countdown=5, exc=TransientError(str(result.get("error", "email failed"))))
    return result


@celery_app.task(bind=True, max_retries=3)  # type: ignore[untyped-decorator]
def send_daily_incident_summary(
    self: Any, summary_date: str | None = None, email_log_id: str | None = None
) -> dict[str, Any]:
    payload = {"date": summary_date} if summary_date else {}
    attempt = int(getattr(self.request, "retries", 0)) + 1
    with SessionLocal() as db:
        service = EmailNotificationService(db, get_settings())
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
    return result
