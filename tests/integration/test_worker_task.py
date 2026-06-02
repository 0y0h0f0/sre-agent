"""Integration tests for the LangGraph worker task."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from apps.api.schemas.alerts import AlertCreateRequest
from apps.worker import tasks
from packages.common.errors import DependencyUnavailableError
from packages.common.settings import Settings
from packages.db.repositories.agent_runs import AgentRunRepository
from packages.db.repositories.incidents import IncidentRepository
from packages.tools.base import ToolResult


def test_build_checkpointer_returns_none_for_sqlite() -> None:
    settings = Settings(database_url="sqlite+pysqlite:///:memory:")
    assert tasks._build_checkpointer(settings) is None


def test_build_checkpointer_fails_closed_for_unreachable_db() -> None:
    # A configured real database that cannot be reached must RAISE rather than
    # silently returning None (which would disable the approval interrupt and
    # auto-approve every L2/L3 action — a fail-open safety hole).
    settings = Settings(
        database_url="postgresql+psycopg://nobody:nobody@127.0.0.1:1/missing"
    )
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
    assert first["status"] == "succeeded"
    assert second["idempotent"] is True
    assert run.status == "succeeded"


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
