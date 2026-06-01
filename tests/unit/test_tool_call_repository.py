from __future__ import annotations

from datetime import UTC, datetime

from apps.api.schemas.alerts import AlertCreateRequest
from packages.db.repositories.agent_runs import AgentRunRepository
from packages.db.repositories.incidents import IncidentRepository
from packages.db.repositories.tool_calls import ToolCallRepository
from packages.tools.base import ToolResult
from packages.tools.metrics import MetricsQuery


def test_tool_call_repository_persists_audit_record(db_session) -> None:
    payload = AlertCreateRequest(
        source="mock",
        fingerprint="fp-tool",
        service="checkout",
        severity="P2",
        alert_name="High5xxAfterDeploy",
        starts_at=datetime(2026, 6, 1, tzinfo=UTC),
    )
    IncidentRepository(db_session).create("inc_tool", payload)
    AgentRunRepository(db_session).create("run_tool", "inc_tool", model_name="fake")
    result = ToolResult(
        status="degraded",
        data={"query": "up"},
        summary="Prometheus unavailable",
        duration_ms=7,
        cache_key="tool:metrics:checkout:test",
        error_message="connection refused",
    )

    repo = ToolCallRepository(db_session)
    repo.create(
        agent_run_id="run_tool",
        node_name="collect_observability",
        tool_name="metrics",
        query=MetricsQuery(
            service="checkout",
            metric_type="error_rate",
            start=datetime(2026, 6, 1, tzinfo=UTC),
            end=datetime(2026, 6, 1, 0, 10, tzinfo=UTC),
        ),
        result=result,
        input_summary="service=checkout, metric=error_rate",
        tool_call_id="tool_1",
    )
    db_session.commit()

    calls = repo.list_for_run("run_tool")
    assert len(calls) == 1
    assert calls[0].tool_call_id == "tool_1"
    assert calls[0].status == "degraded"
    assert calls[0].output_summary == "Prometheus unavailable"
