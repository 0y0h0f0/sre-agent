from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from apps.api.schemas.alerts import AlertCreateRequest, AlertCreateResponse
from apps.api.schemas.common import IncidentStatus
from packages.common.errors import DependencyUnavailableError
from packages.common.ids import new_id
from packages.common.metrics import grafana_webhook_ignored_total, grafana_webhook_ingest_total
from packages.common.settings import Settings
from packages.db.models import Incident
from packages.db.repositories.agent_runs import AgentRunRepository
from packages.db.repositories.false_positive_patterns import FalsePositivePatternRepository
from packages.db.repositories.incidents import IncidentRepository

TaskEnqueue = Callable[[str, str], str]
NotificationTaskEnqueue = Callable[[str, dict[str, Any]], str]


class AlertService:
    def __init__(
        self,
        db: Session,
        settings: Settings,
        enqueue_diagnosis: TaskEnqueue,
        enqueue_notification: NotificationTaskEnqueue | None = None,
    ) -> None:
        self.db = db
        self.settings = settings
        self.enqueue_diagnosis = enqueue_diagnosis
        self.enqueue_notification = enqueue_notification
        self.incidents = IncidentRepository(db)
        self.agent_runs = AgentRunRepository(db)
        self.fpp = FalsePositivePatternRepository(db)

    def create_alert(self, payload: AlertCreateRequest) -> AlertCreateResponse:
        # Phase 5: check for suppressed NFA patterns before creating incident
        suppressed = self.fpp.should_suppress(
            payload.fingerprint, threshold=self.settings.nfa_auto_suppress_threshold
        )

        existing = self.incidents.get_open_by_fingerprint(payload.fingerprint)
        if existing is not None:
            return self._deduplicated_response(existing)

        incident_id = new_id("inc_")
        agent_run_id = new_id("run_")
        incident = self.incidents.create(incident_id, payload)
        if suppressed:
            incident.severity = "P4"
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
            # Move the incident to a terminal state. Leaving it OPEN would make
            # every future alert with the same fingerprint deduplicate onto this
            # incident that will never be diagnosed (no task was ever queued).
            incident.status = IncidentStatus.FAILED.value
            self.db.commit()
            raise DependencyUnavailableError("celery", "failed to enqueue diagnosis task") from exc

        self.agent_runs.set_task_id(agent_run_id, celery_task_id)
        self.db.commit()
        self._enqueue_notification("new_incident", {"incident_id": incident_id})
        return AlertCreateResponse(
            incident_id=incident_id,
            agent_run_id=agent_run_id,
            celery_task_id=celery_task_id,
            status="queued",
            deduplicated=False,
        )

    def _enqueue_notification(self, notification_type: str, payload: dict[str, Any]) -> None:
        if self.enqueue_notification is None:
            return
        try:
            self.enqueue_notification(notification_type, payload)
        except Exception:
            # Notification enqueue failures must not block alert ingestion.
            return

    def ingest_grafana_alert(self, raw_payload: dict[str, Any]) -> AlertCreateResponse | None:
        """Ingest a Grafana unified alerting webhook payload.

        Returns AlertCreateResponse on successful ingest, or None when ingest
        is disabled (caller should return 204).

        Raises ValueError for malformed payloads (caller should return 400).
        """
        # 1. Feature gate check.
        if not self.settings.grafana_alert_ingest_enabled:
            grafana_webhook_ignored_total.labels(reason="disabled").inc()
            return None

        # 2. Basic payload validation.
        if not isinstance(raw_payload, dict):
            grafana_webhook_ingest_total.labels(status="malformed").inc()
            raise ValueError("payload must be a JSON object")

        if "alerts" not in raw_payload or not isinstance(raw_payload["alerts"], list):
            grafana_webhook_ingest_total.labels(status="malformed").inc()
            raise ValueError("missing 'alerts' field in Grafana payload")

        # 3. Parse via Grafana schema.
        from apps.api.schemas.alerts import grafana_to_alert
        parsed = grafana_to_alert(raw_payload)
        parsed["source"] = "grafana"
        request = AlertCreateRequest.model_validate(parsed)

        # 4. Create alert (dedup handled internally).
        response = self.create_alert(request)
        grafana_webhook_ingest_total.labels(status="success").inc()
        return response

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
