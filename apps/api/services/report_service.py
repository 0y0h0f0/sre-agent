"""Incident report read/regeneration service."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sqlalchemy.orm import Session

from apps.api.schemas.reports import IncidentReportResponse
from packages.common.errors import ConflictError, NotFoundError
from packages.db.models import Action, AgentRun, EvidenceItem, Incident, IncidentReport
from packages.db.repositories.agent_runs import AgentRunRepository
from packages.db.repositories.incidents import IncidentRepository
from packages.db.repositories.incidents_read import IncidentReadRepository
from packages.db.repositories.reports import IncidentReportRepository

NotificationTaskEnqueue = Callable[[str, dict[str, Any]], str]


class ReportService:
    """Owns latest report lookup and append-only report regeneration."""

    def __init__(
        self, db: Session, enqueue_notification: NotificationTaskEnqueue | None = None
    ) -> None:
        self.db = db
        self.enqueue_notification = enqueue_notification
        self.incidents = IncidentRepository(db)
        self.reads = IncidentReadRepository(db)
        self.agent_runs = AgentRunRepository(db)
        self.reports = IncidentReportRepository(db)

    def get_latest(self, incident_id: str) -> IncidentReportResponse:
        """Return the latest persisted report version for an incident."""
        self._require_incident(incident_id)
        report = self.reports.get_latest_for_incident(incident_id)
        if report is None:
            raise NotFoundError("report", incident_id)
        return self._schema(report)

    def regenerate(self, incident_id: str) -> IncidentReportResponse:
        """Create a new report version from the latest run and persisted facts.

        Regeneration is append-only: it uses ``next_version`` and never overwrites
        older incident_reports rows.
        """
        incident = self._require_incident(incident_id)
        run = self.agent_runs.get_latest_for_incident(incident_id)
        if run is None:
            raise ConflictError(
                "incident has no agent run to build a report from",
                details={"incident_id": incident_id},
            )

        evidence = list(self.reads.list_evidence(incident_id))
        actions = list(self.reads.list_actions(incident_id))
        state_report = _dict_value(run.state.get("incident_report"))
        version = self.reports.next_version(incident_id)

        # Prefer the structured report from the run state when present, but
        # fall back to durable incident/evidence/action rows so regeneration
        # still works for older or partial runs.
        root_cause = _string_value(state_report.get("root_cause")) or _root_cause(incident, run)
        impact = _string_value(state_report.get("impact")) or _impact(incident)
        timeline = _record_list(state_report.get("timeline")) or _timeline(
            incident, evidence, actions
        )
        report_actions = _record_list(state_report.get("actions")) or _actions(actions)
        state_follow_ups = _follow_ups(state_report.get("follow_ups"))
        follow_ups: list[dict[str, Any] | str] = (
            state_follow_ups if state_follow_ups else _default_followups(incident)
        )
        body_markdown = _body_markdown(
            incident=incident,
            version=version,
            root_cause=root_cause,
            impact=impact,
            timeline=timeline,
            actions=report_actions,
            follow_ups=follow_ups,
        )

        report = self.reports.create(
            incident_id=incident_id,
            agent_run_id=run.agent_run_id,
            version=version,
            root_cause=root_cause,
            impact=impact,
            timeline=timeline,
            actions=report_actions,
            follow_ups=follow_ups,
            body_markdown=body_markdown,
        )
        self.db.commit()
        # Notification failure must not roll back the new report version.
        self._enqueue_notification("incident_report", {"report_id": report.report_id})
        return self._schema(report)

    def _enqueue_notification(self, notification_type: str, payload: dict[str, Any]) -> None:
        """Best-effort report notification enqueue."""
        if self.enqueue_notification is None:
            return
        try:
            self.enqueue_notification(notification_type, payload)
        except Exception:
            return

    def _require_incident(self, incident_id: str) -> Incident:
        incident = self.incidents.get_by_public_id(incident_id)
        if incident is None:
            raise NotFoundError("incident", incident_id)
        return incident

    def _schema(self, report: IncidentReport) -> IncidentReportResponse:
        """Map the persisted report plus current evidence IDs to API schema."""
        evidence_ids = [item.evidence_id for item in self.reads.list_evidence(report.incident_id)]
        return IncidentReportResponse(
            report_id=report.report_id,
            incident_id=report.incident_id,
            agent_run_id=report.agent_run_id,
            version=report.version,
            root_cause=report.root_cause,
            impact=report.impact,
            timeline=report.timeline,
            actions=report.actions,
            follow_ups=report.follow_ups,
            evidence_ids=evidence_ids,
            body_markdown=report.body_markdown,
            created_at=report.created_at,
        )


def _root_cause(incident: Incident, run: AgentRun) -> str:
    """Resolve root cause text from incident/run fallback sources."""
    state_root = _dict_value(run.state.get("root_cause"))
    return (
        incident.root_cause_summary
        or _string_value(state_root.get("summary"))
        or "Root cause has not been determined"
    )


def _impact(incident: Incident) -> str:
    return f"{incident.severity} incident affecting {incident.service}"


def _timeline(
    incident: Incident,
    evidence: list[EvidenceItem],
    actions: list[Action],
) -> list[dict[str, Any]]:
    """Build a simple timeline from alert time, evidence timestamps, and actions."""
    entries: list[dict[str, Any]] = [
        {"time": incident.starts_at.isoformat(), "event": f"{incident.alert_name} fired"}
    ]
    for item in evidence[:8]:
        if item.timestamp is not None:
            entries.append({"time": item.timestamp.isoformat(), "event": item.title})
    for action in actions:
        entries.append(
            {
                "time": action.created_at.isoformat(),
                "event": f"Action {action.type} is {action.status}",
            }
        )
    return entries


def _actions(actions: list[Action]) -> list[dict[str, Any]]:
    """Project action rows into report action entries."""
    return [
        {
            "action_id": action.action_id,
            "type": action.type,
            "risk_level": action.risk_level,
            "status": action.status,
            "reason": action.reason,
            "rollback_plan": action.rollback_plan,
        }
        for action in actions
    ]


def _default_followups(incident: Incident) -> list[dict[str, Any] | str]:
    return [
        {"item": f"Review alert thresholds for {incident.service}", "status": "open"},
        {"item": "Update the runbook with confirmed evidence", "status": "open"},
    ]


def _body_markdown(
    *,
    incident: Incident,
    version: int,
    root_cause: str,
    impact: str,
    timeline: list[dict[str, Any]],
    actions: list[dict[str, Any]],
    follow_ups: list[dict[str, Any] | str],
) -> str:
    """Render a deterministic Markdown body for regenerated reports."""
    lines = [
        f"# Incident report v{version}",
        "",
        f"Incident: {incident.incident_id}",
        f"Service: {incident.service}",
        "",
        "## Root cause",
        root_cause,
        "",
        "## Impact",
        impact,
        "",
        "## Timeline",
    ]
    lines.extend(f"- {entry}" for entry in timeline)
    lines.extend(["", "## Actions"])
    lines.extend(f"- {entry}" for entry in actions)
    lines.extend(["", "## Follow-ups"])
    lines.extend(f"- {entry}" for entry in follow_ups)
    return "\n".join(lines)


def _dict_value(value: Any) -> dict[str, Any]:
    """Return dict values only, shielding older state snapshots."""
    return value if isinstance(value, dict) else {}


def _record_list(value: Any) -> list[dict[str, Any]]:
    """Return list entries that are JSON object records."""
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _follow_ups(value: Any) -> list[dict[str, Any] | str]:
    """Return follow-up entries that match the API schema."""
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict) or isinstance(item, str)]


def _string_value(value: Any) -> str:
    """Return string values only."""
    return value if isinstance(value, str) else ""
