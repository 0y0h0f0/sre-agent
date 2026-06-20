"""Repository for incident_reports table."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from packages.common.ids import new_id
from packages.db.models import IncidentReport


class IncidentReportRepository:
    """Data access for append-only incident report versions."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def create(
        self,
        *,
        incident_id: str,
        agent_run_id: str,
        version: int,
        root_cause: str,
        impact: str,
        timeline: list[dict[str, Any]],
        actions: list[dict[str, Any]],
        follow_ups: list[dict[str, Any] | str],
        body_markdown: str,
    ) -> IncidentReport:
        """Create a report version row.

        Callers compute ``version`` with ``next_version`` and commit the
        transaction. The database unique constraint protects against duplicate
        versions for the same incident.
        """
        report = IncidentReport(
            report_id=new_id("rpt_"),
            incident_id=incident_id,
            agent_run_id=agent_run_id,
            version=version,
            root_cause=root_cause,
            impact=impact,
            timeline=timeline,
            actions=actions,
            follow_ups=follow_ups,
            body_markdown=body_markdown,
        )
        self.db.add(report)
        return report

    def get_latest_for_incident(self, incident_id: str) -> IncidentReport | None:
        """Return the highest report version for an incident."""
        stmt = (
            select(IncidentReport)
            .where(IncidentReport.incident_id == incident_id)
            .order_by(IncidentReport.version.desc(), IncidentReport.id.desc())
            .limit(1)
        )
        return self.db.scalar(stmt)

    def get_by_public_id(self, report_id: str) -> IncidentReport | None:
        stmt = select(IncidentReport).where(IncidentReport.report_id == report_id)
        return self.db.scalar(stmt)

    def next_version(self, incident_id: str) -> int:
        """Compute the next report version number for append-only regeneration."""
        latest = self.get_latest_for_incident(incident_id)
        if latest is None:
            return 1
        return latest.version + 1

    def list_for_incident(self, incident_id: str) -> Sequence[IncidentReport]:
        """Return report versions newest first."""
        stmt = (
            select(IncidentReport)
            .where(IncidentReport.incident_id == incident_id)
            .order_by(IncidentReport.version.desc(), IncidentReport.id.desc())
        )
        return self.db.scalars(stmt).all()
