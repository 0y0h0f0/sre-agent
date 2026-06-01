from __future__ import annotations

from collections.abc import Callable

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from apps.api.schemas.alerts import AlertCreateRequest, AlertCreateResponse
from packages.common.errors import DependencyUnavailableError
from packages.common.ids import new_id
from packages.common.settings import Settings
from packages.db.models import Incident
from packages.db.repositories.agent_runs import AgentRunRepository
from packages.db.repositories.incidents import IncidentRepository

TaskEnqueue = Callable[[str, str], str]


class AlertService:
    def __init__(self, db: Session, settings: Settings, enqueue_diagnosis: TaskEnqueue) -> None:
        self.db = db
        self.settings = settings
        self.enqueue_diagnosis = enqueue_diagnosis
        self.incidents = IncidentRepository(db)
        self.agent_runs = AgentRunRepository(db)

    def create_alert(self, payload: AlertCreateRequest) -> AlertCreateResponse:
        existing = self.incidents.get_open_by_fingerprint(payload.fingerprint)
        if existing is not None:
            return self._deduplicated_response(existing)

        incident_id = new_id("inc_")
        agent_run_id = new_id("run_")
        incident = self.incidents.create(incident_id, payload)
        self.agent_runs.create(
            agent_run_id,
            incident.incident_id,
            model_name=self.settings.llm_model,
        )
        try:
            self.db.commit()
        except IntegrityError:
            self.db.rollback()
            existing = self.incidents.get_open_by_fingerprint(payload.fingerprint)
            if existing is not None:
                return self._deduplicated_response(existing)
            raise

        try:
            celery_task_id = self.enqueue_diagnosis(incident_id, agent_run_id)
        except Exception as exc:  # pragma: no cover - specific clients vary
            self.agent_runs.mark_enqueue_failed(agent_run_id, str(exc))
            self.db.commit()
            raise DependencyUnavailableError("celery", "failed to enqueue diagnosis task") from exc

        self.agent_runs.set_task_id(agent_run_id, celery_task_id)
        self.db.commit()
        return AlertCreateResponse(
            incident_id=incident_id,
            agent_run_id=agent_run_id,
            celery_task_id=celery_task_id,
            status="queued",
            deduplicated=False,
        )

    def _deduplicated_response(self, incident: Incident) -> AlertCreateResponse:
        latest_run = self.agent_runs.get_latest_for_incident(incident.incident_id)
        if latest_run is None:
            agent_run_id = ""
            celery_task_id = ""
        else:
            agent_run_id = latest_run.agent_run_id
            celery_task_id = latest_run.celery_task_id or ""
        return AlertCreateResponse(
            incident_id=incident.incident_id,
            agent_run_id=agent_run_id,
            celery_task_id=celery_task_id,
            status=incident.status,
            deduplicated=True,
        )
