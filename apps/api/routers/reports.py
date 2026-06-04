from __future__ import annotations

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from apps.api.dependencies import (
    NotificationTaskEnqueue,
    get_db,
    get_notification_task_enqueue,
)
from apps.api.schemas.reports import IncidentReportResponse
from apps.api.services.report_service import ReportService

router = APIRouter(prefix="/api/incidents", tags=["reports"])


@router.get("/{incident_id}/report", response_model=IncidentReportResponse)
def get_incident_report(
    incident_id: str,
    db: Session = Depends(get_db),
) -> IncidentReportResponse:
    return ReportService(db).get_latest(incident_id)


@router.post(
    "/{incident_id}/report/regenerate",
    response_model=IncidentReportResponse,
    status_code=status.HTTP_201_CREATED,
)
def regenerate_incident_report(
    incident_id: str,
    db: Session = Depends(get_db),
    enqueue_notification: NotificationTaskEnqueue = Depends(get_notification_task_enqueue),
) -> IncidentReportResponse:
    return ReportService(db, enqueue_notification).regenerate(incident_id)
