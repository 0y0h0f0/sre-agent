"""Integration tests for the LangGraph worker task."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from apps.api.schemas.alerts import AlertCreateRequest
from apps.worker import tasks
from packages.db.repositories.agent_runs import AgentRunRepository
from packages.db.repositories.incidents import IncidentRepository
from packages.tools.base import ToolResult


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
