"""Verify the worker records tool calls during graph execution."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from apps.api.schemas.alerts import AlertCreateRequest
from apps.worker import tasks
from packages.db.repositories.agent_runs import AgentRunRepository
from packages.db.repositories.incidents import IncidentRepository
from packages.db.repositories.tool_calls import ToolCallRepository
from packages.tools.base import ToolResult


class _FakeTool:
    def __init__(self, **kw: Any) -> None:
        pass

    name: str = "fake"

    def run(self, query: Any) -> ToolResult:
        return ToolResult(
            status="succeeded", data={}, summary="fake ok", evidence=[], duration_ms=1
        )


def test_worker_records_nodes_and_tool_calls(monkeypatch, db_session) -> None:
    for tool_name in ("MetricsTool", "LogsTool", "TraceTool", "GitChangeTool", "RunbookSearchTool"):
        monkeypatch.setattr(tasks, tool_name, _FakeTool)

    payload = AlertCreateRequest(
        source="mock",
        fingerprint="fp-audit",
        service="checkout",
        severity="P2",
        alert_name="High5xxAfterDeploy",
        starts_at=datetime(2026, 6, 1, tzinfo=UTC),
    )
    IncidentRepository(db_session).create("inc_audit", payload)
    AgentRunRepository(db_session).create("run_audit", "inc_audit", model_name="fake")
    db_session.commit()

    result = tasks.run_incident_diagnosis_logic(db_session, "inc_audit", "run_audit")
    assert result["status"] == "succeeded"

    calls = ToolCallRepository(db_session).list_for_run("run_audit")
    assert len(calls) >= 4

    run = AgentRunRepository(db_session).get_by_public_id("run_audit")
    assert run is not None
    assert run.status == "succeeded"
    assert run.state.get("service_name") == "checkout"
