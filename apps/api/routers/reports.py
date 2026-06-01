from __future__ import annotations

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from apps.api.dependencies import get_db
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
) -> IncidentReportResponse:
    return ReportService(db).regenerate(incident_id)
