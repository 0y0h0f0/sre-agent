"""Integration tests for the LangGraph worker task."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from apps.api.schemas.alerts import AlertCreateRequest
from apps.worker import tasks
from packages.common.errors import DependencyUnavailableError
from packages.common.settings import Settings
from packages.db.models import EmailLog
from packages.db.repositories.agent_runs import AgentRunRepository
from packages.db.repositories.email_logs import EmailLogRepository
from packages.db.repositories.incidents import IncidentRepository
from packages.tools.base import ToolResult


def test_build_checkpointer_returns_none_for_sqlite() -> None:
    settings = Settings(database_url="sqlite+pysqlite:///:memory:")
    assert tasks._build_checkpointer(settings) is None


def test_postgres_saver_conn_string_strips_sqlalchemy_driver() -> None:
    assert (
        tasks._postgres_saver_conn_string(
            "postgresql+psycopg://sre:sre@postgres:5432/sre?sslmode=disable"
        )
        == "postgresql://sre:sre@postgres:5432/sre?sslmode=disable"
    )
    assert (
        tasks._postgres_saver_conn_string(
            "postgres+psycopg://sre:sre@postgres:5432/sre"
        )
        == "postgres://sre:sre@postgres:5432/sre"
    )
    assert (
        tasks._postgres_saver_conn_string("postgresql://sre:sre@postgres:5432/sre")
        == "postgresql://sre:sre@postgres:5432/sre"
    )
    assert (
        tasks._postgres_saver_conn_string("host=postgres dbname=sre")
        == "host=postgres dbname=sre"
    )


def test_build_checkpointer_fails_closed_for_unreachable_db() -> None:
    # A configured real database that cannot be reached must RAISE rather than
    # silently returning None (which would disable the approval interrupt and
    # auto-approve every L2/L3 action — a fail-open safety hole).
    settings = Settings(database_url="postgresql+psycopg://nobody:nobody@127.0.0.1:1/missing")
    with pytest.raises(DependencyUnavailableError):
        tasks._build_checkpointer(settings)


class _FakeTool:
    def __init__(self, **kw: Any) -> None:
        pass

    name: str = "fake"

    def run(self, query: Any) -> ToolResult:
        return ToolResult(
            status="succeeded", data={}, summary="fake ok", evidence=[], duration_ms=1
        )


def test_worker_task_idempotent(monkeypatch, db_session) -> None:
    for tool_name in ("MetricsTool", "LogsTool", "TraceTool", "GitChangeTool", "RunbookSearchTool"):
        monkeypatch.setattr(tasks, tool_name, _FakeTool)
    # Run on the intentional no-checkpointer (auto-approve) path; this test has
    # no real Postgres, and _build_checkpointer now fails closed for real DBs.
    monkeypatch.setattr(tasks, "_build_checkpointer", lambda settings: None)

    payload = AlertCreateRequest(
        source="mock",
        fingerprint="fp-worker",
        service="checkout",
        severity="P2",
        alert_name="High5xxAfterDeploy",
        starts_at=datetime(2026, 6, 1, tzinfo=UTC),
    )
    IncidentRepository(db_session).create("inc_worker", payload)
    runs = AgentRunRepository(db_session)
    runs.create("run_worker", "inc_worker", model_name="fake")
    db_session.commit()

    first = tasks.run_incident_diagnosis_logic(db_session, "inc_worker", "run_worker")
    second = tasks.run_incident_diagnosis_logic(db_session, "inc_worker", "run_worker")

    run = runs.get_by_public_id("run_worker")
    incident = IncidentRepository(db_session).get_by_public_id("inc_worker")
    assert first["status"] == "succeeded"
    assert second["idempotent"] is True
    assert run.status == "succeeded"
    assert incident is not None
    assert incident.root_cause_summary


@pytest.mark.parametrize("in_flight_status", ["running", "waiting_approval"])
def test_diagnosis_skips_in_flight_run(monkeypatch, db_session, in_flight_status) -> None:
    """A redelivered Celery task for an already in-flight run must NOT re-run.

    Celery's at-least-once delivery can hand the same task to a second worker
    while the first is still RUNNING or WAITING_APPROVAL. Re-running the graph
    would duplicate execution and approvals, so the logic must short-circuit
    without ever constructing an AgentRunner.
    """

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("AgentRunner must not run for an in-flight task")

    monkeypatch.setattr(tasks, "AgentRunner", _boom)

    payload = AlertCreateRequest(
        source="mock",
        fingerprint="fp-inflight",
        service="checkout",
        severity="P2",
        alert_name="High5xxAfterDeploy",
        starts_at=datetime(2026, 6, 1, tzinfo=UTC),
    )
    IncidentRepository(db_session).create("inc_inflight", payload)
    runs = AgentRunRepository(db_session)
    run = runs.create("run_inflight", "inc_inflight", model_name="fake")
    run.status = in_flight_status
    db_session.commit()

    result = tasks.run_incident_diagnosis_logic(db_session, "inc_inflight", "run_inflight")

    assert result["idempotent"] is True
    assert result["status"] == in_flight_status
    assert runs.get_by_public_id("run_inflight").status == in_flight_status


def test_notify_approval_requests_enqueues_each_id(monkeypatch) -> None:
    calls: list[tuple[str, dict[str, str]]] = []

    def _fake_enqueue(notification_type: str, payload: dict[str, str]) -> str:
        calls.append((notification_type, payload))
        return "email-task"

    monkeypatch.setattr(tasks, "enqueue_email_notification_task", _fake_enqueue)

    tasks._notify_approval_requests({"approval_status": {"approval_ids": ["apv_1", "apv_2"]}})

    assert calls == [
        ("approval_request", {"approval_id": "apv_1"}),
        ("approval_request", {"approval_id": "apv_2"}),
    ]


def test_notify_diagnosis_complete_skips_existing_email_log(monkeypatch, db_session) -> None:
    calls: list[tuple[str, dict[str, str]]] = []

    def _fake_enqueue(notification_type: str, payload: dict[str, str]) -> str:
        calls.append((notification_type, payload))
        return "email-task"

    monkeypatch.setattr(tasks, "enqueue_email_notification_task", _fake_enqueue)
    EmailLogRepository(db_session).create(
        notification_type="diagnosis_complete",
        recipients=["sre@example.com"],
        subject="Diagnosis Complete",
        related_incident_id="inc_existing",
        related_agent_run_id="run_existing",
    )
    db_session.commit()

    tasks._notify_diagnosis_complete("inc_existing", "run_existing", db=db_session)

    assert calls == []


def test_notify_report_generated_enqueues_report_id(monkeypatch) -> None:
    calls: list[tuple[str, dict[str, str]]] = []

    def _fake_enqueue(notification_type: str, payload: dict[str, str]) -> str:
        calls.append((notification_type, payload))
        return "email-task"

    monkeypatch.setattr(tasks, "enqueue_email_notification_task", _fake_enqueue)

    tasks._notify_report_generated({"incident_report": {"report_id": "rpt_1"}})

    assert calls == [("incident_report", {"report_id": "rpt_1"})]


def test_enqueue_email_notification_task_marks_log_failed_when_delay_fails(
    monkeypatch, db_session
) -> None:
    payload = AlertCreateRequest(
        source="mock",
        fingerprint="fp-email-enqueue",
        service="checkout",
        severity="P2",
        alert_name="High5xxAfterDeploy",
        starts_at=datetime(2026, 6, 1, tzinfo=UTC),
    )
    IncidentRepository(db_session).create("inc_email_enqueue", payload)
    db_session.commit()

    test_session_local = sessionmaker(
        bind=db_session.get_bind(),
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
    )
    monkeypatch.setattr(tasks, "SessionLocal", test_session_local)
    monkeypatch.setattr(
        tasks,
        "get_settings",
        lambda: Settings(
            database_url="sqlite+pysqlite:///:memory:",
            sre_email_list="sre@example.com",
            web_base_url="http://console.local",
        ),
    )

    def _delay(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("broker down")

    monkeypatch.setattr(tasks.send_email_notification, "delay", _delay)

    with pytest.raises(RuntimeError, match="broker down"):
        tasks.enqueue_email_notification_task("new_incident", {"incident_id": "inc_email_enqueue"})

    db_session.expire_all()
    log = db_session.scalar(
        select(EmailLog).where(EmailLog.related_incident_id == "inc_email_enqueue")
    )
    assert log is not None
    assert log.status == "failed"
    assert log.attempts == 0
    assert log.last_error is not None
    assert "notification enqueue failed: broker down" in log.last_error
