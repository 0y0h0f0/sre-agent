"""Incident read and manual diagnosis service."""

from __future__ import annotations

from collections.abc import Callable

from sqlalchemy.orm import Session

from apps.api.schemas.agent_runs import AgentRunSummary
from apps.api.schemas.common import (
    ActionStatus,
    ActionSummary,
    AgentRunStatus,
    EvidenceItem,
    IncidentStatus,
    PaginatedResponse,
    RiskLevel,
    RootCause,
    Severity,
)
from apps.api.schemas.incidents import (
    DiagnoseRequest,
    DiagnoseResponse,
    IncidentDetailResponse,
    IncidentListItem,
)
from packages.common.errors import ConflictError, DependencyUnavailableError, NotFoundError
from packages.common.ids import new_id
from packages.common.settings import Settings
from packages.db.models import Action, Incident
from packages.db.models import EvidenceItem as EvidenceModel
from packages.db.repositories.agent_runs import AgentRunRepository
from packages.db.repositories.incidents import IncidentRepository
from packages.db.repositories.incidents_read import IncidentReadRepository

TaskEnqueue = Callable[[str, str], str]


class IncidentService:
    """Owns incident API business logic above repository queries."""

    def __init__(
        self,
        db: Session,
        settings: Settings,
        enqueue_diagnosis: TaskEnqueue | None = None,
    ) -> None:
        self.db = db
        self.settings = settings
        self.enqueue_diagnosis = enqueue_diagnosis
        self.incidents = IncidentRepository(db)
        self.agent_runs = AgentRunRepository(db)
        self.reads = IncidentReadRepository(db)

    def list_incidents(
        self,
        *,
        status: str | None,
        service: str | None,
        severity: str | None,
        page: int,
        page_size: int,
    ) -> PaginatedResponse:
        incidents, total = self.incidents.list_with_count(
            status=status,
            service=service,
            severity=severity,
            page=page,
            page_size=page_size,
        )
        return PaginatedResponse(
            items=[self._list_item(incident) for incident in incidents],
            total=total,
            page=page,
            page_size=page_size,
        )

    def get_detail(self, incident_id: str) -> IncidentDetailResponse:
        """Return incident detail with evidence/action API projections."""
        incident = self._require_incident(incident_id)
        evidence = [self._evidence_item(item) for item in self.reads.list_evidence(incident_id)]
        actions = [self._action_summary(action) for action in self.reads.list_actions(incident_id)]
        root_cause = None
        if incident.root_cause_summary:
            root_cause = RootCause(summary=incident.root_cause_summary)
        return IncidentDetailResponse(
            incident_id=incident.incident_id,
            service=incident.service,
            severity=Severity(incident.severity),
            status=IncidentStatus(incident.status),
            alert=self.incidents.alert_payload(incident),
            root_cause=root_cause,
            evidence=evidence,
            recommended_actions=actions,
        )

    def trigger_diagnosis(self, incident_id: str, request: DiagnoseRequest) -> DiagnoseResponse:
        """Create a new diagnosis run and enqueue it.

        ``force=False`` protects the worker/checkpointer path from concurrent
        active runs for the same incident. ``force=True`` creates an additional
        run but does not delete or mutate the existing one.
        """
        incident = self._require_incident(incident_id)
        active = self.agent_runs.get_active_for_incident(incident_id)
        if active is not None and not request.force:
            raise ConflictError(
                "incident already has an active diagnosis run",
                details={"agent_run_id": active.agent_run_id, "status": active.status},
            )

        agent_run_id = new_id("run_")
        self.agent_runs.create(
            agent_run_id, incident.incident_id, model_name=self.settings.llm_model
        )
        # Commit the run before enqueue so Celery can read it immediately.
        self.db.commit()

        if self.enqueue_diagnosis is None:
            raise DependencyUnavailableError("celery", "diagnosis enqueue is not configured")

        try:
            celery_task_id = self.enqueue_diagnosis(incident_id, agent_run_id)
        except Exception as exc:  # pragma: no cover - specific clients vary
            # The incident already existed before this manual run. Mark only the
            # new run failed; do not fail the whole incident for a retry enqueue
            # problem.
            self.agent_runs.mark_enqueue_failed(agent_run_id, str(exc))
            self.db.commit()
            raise DependencyUnavailableError("celery", "failed to enqueue diagnosis task") from exc

        self.agent_runs.set_task_id(agent_run_id, celery_task_id)
        self.db.commit()
        return DiagnoseResponse(
            incident_id=incident_id,
            agent_run_id=agent_run_id,
            celery_task_id=celery_task_id,
            status="queued",
        )

    def list_runs(self, incident_id: str) -> list[AgentRunSummary]:
        """List runs after proving the incident exists."""
        self._require_incident(incident_id)
        return [
            AgentRunSummary(
                agent_run_id=run.agent_run_id,
                incident_id=run.incident_id,
                status=AgentRunStatus(run.status),
                celery_task_id=run.celery_task_id,
                created_at=run.created_at,
                updated_at=run.updated_at,
            )
            for run in self.agent_runs.list_for_incident(incident_id)
        ]

    def _require_incident(self, incident_id: str) -> Incident:
        incident = self.incidents.get_by_public_id(incident_id)
        if incident is None:
            raise NotFoundError("incident", incident_id)
        return incident

    def _list_item(self, incident: Incident) -> IncidentListItem:
        return IncidentListItem(
            incident_id=incident.incident_id,
            service=incident.service,
            severity=Severity(incident.severity),
            status=IncidentStatus(incident.status),
            alert_name=incident.alert_name,
            root_cause_summary=incident.root_cause_summary,
            created_at=incident.created_at,
            updated_at=incident.updated_at,
        )

    def _evidence_item(self, item: EvidenceModel) -> EvidenceItem:
        """Map persisted evidence to the public API shape.

        Runbook evidence may store source_path/metadata either at the top level
        or under payload.payload depending on which node/tool produced it.
        """
        return EvidenceItem(
            evidence_id=item.evidence_id,
            type=item.type,
            source=item.source,
            source_id=item.source_id,
            source_path=_source_path(item.payload),
            title=item.title,
            excerpt=item.excerpt,
            metadata=_metadata(item.payload),
            confidence=item.confidence,
            timestamp=item.timestamp,
        )

    def _action_summary(self, action: Action) -> ActionSummary:
        return ActionSummary(
            action_id=action.action_id,
            type=action.type,
            risk_level=RiskLevel(action.risk_level),
            status=ActionStatus(action.status),
            reason=action.reason,
            rollback_plan=action.rollback_plan,
        )


def _source_path(payload: dict[str, object]) -> str | None:
    """Extract runbook/source path from historical payload shapes."""
    nested = payload.get("payload")
    if isinstance(nested, dict) and nested.get("source_path"):
        return str(nested["source_path"])
    value = payload.get("source_path")
    return str(value) if value else None


def _metadata(payload: dict[str, object]) -> dict[str, object]:
    """Extract evidence metadata from historical payload shapes."""
    nested = payload.get("payload")
    if isinstance(nested, dict) and isinstance(nested.get("metadata"), dict):
        return dict(nested["metadata"])
    metadata = payload.get("metadata")
    return dict(metadata) if isinstance(metadata, dict) else {}
