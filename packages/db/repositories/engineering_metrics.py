"""Read-only aggregation helpers for project engineering metrics."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from packages.db.models import (
    Action,
    AgentRun,
    AgentRunNode,
    Approval,
    EvalRun,
    EvidenceItem,
    Incident,
    IncidentReport,
    ToolCall,
)


class EngineeringMetricsRepository:
    """Repository boundary for project-level metric aggregation."""

    def __init__(self, db: Session) -> None:
        self._db = db

    def list_agent_runs(self, *, since: datetime) -> list[AgentRun]:
        return list(
            self._db.scalars(
                select(AgentRun).where(AgentRun.created_at >= since)
            ).all()
        )

    def list_incidents(self, *, since: datetime) -> list[Incident]:
        return list(
            self._db.scalars(
                select(Incident).where(Incident.created_at >= since)
            ).all()
        )

    def list_tool_calls(self, *, since: datetime) -> list[ToolCall]:
        return list(
            self._db.scalars(
                select(ToolCall).where(ToolCall.created_at >= since)
            ).all()
        )

    def list_agent_run_nodes(self, *, since: datetime) -> list[AgentRunNode]:
        return list(
            self._db.scalars(
                select(AgentRunNode).where(AgentRunNode.created_at >= since)
            ).all()
        )

    def list_actions(self, *, since: datetime) -> list[Action]:
        return list(
            self._db.scalars(
                select(Action).where(Action.created_at >= since)
            ).all()
        )

    def list_approvals(self, *, since: datetime) -> list[Approval]:
        return list(
            self._db.scalars(
                select(Approval).where(Approval.requested_at >= since)
            ).all()
        )

    def list_reports(self, *, since: datetime) -> list[IncidentReport]:
        return list(
            self._db.scalars(
                select(IncidentReport).where(IncidentReport.created_at >= since)
            ).all()
        )

    def list_evidence_items(self, *, since: datetime) -> list[EvidenceItem]:
        return list(
            self._db.scalars(
                select(EvidenceItem).where(EvidenceItem.created_at >= since)
            ).all()
        )

    def latest_eval_run(self, *, suite: str, since: datetime) -> EvalRun | None:
        return self._db.scalars(
            select(EvalRun)
            .where(
                EvalRun.suite == suite,
                EvalRun.status == "succeeded",
                EvalRun.created_at >= since,
            )
            .order_by(EvalRun.created_at.desc())
            .limit(1)
        ).one_or_none()
