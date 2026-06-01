from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from apps.api.dependencies import TaskEnqueue, get_app_settings, get_db, get_task_enqueue
from apps.api.schemas.agent_runs import AgentRunSummary
from apps.api.schemas.common import PaginatedResponse
from apps.api.schemas.incidents import (
    DiagnoseRequest,
    DiagnoseResponse,
    IncidentDetailResponse,
)
from apps.api.services.incident_service import IncidentService
from packages.common.settings import Settings

router = APIRouter(prefix="/api/incidents", tags=["incidents"])
Page = Annotated[int, Query(ge=1)]
PageSize = Annotated[int, Query(ge=1, le=100)]


@router.get("", response_model=PaginatedResponse)
def list_incidents(
    status: str | None = None,
    service: str | None = None,
    severity: str | None = None,
    page: Page = 1,
    page_size: PageSize = 20,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_app_settings),
    enqueue_diagnosis: TaskEnqueue = Depends(get_task_enqueue),
) -> PaginatedResponse:
    return IncidentService(db, settings, enqueue_diagnosis).list_incidents(
        status=status,
        service=service,
        severity=severity,
        page=page,
        page_size=page_size,
    )


@router.get("/{incident_id}/runs", response_model=list[AgentRunSummary])
def list_incident_runs(
    incident_id: str,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_app_settings),
    enqueue_diagnosis: TaskEnqueue = Depends(get_task_enqueue),
) -> list[AgentRunSummary]:
    return IncidentService(db, settings, enqueue_diagnosis).list_runs(incident_id)


@router.post("/{incident_id}/diagnose", response_model=DiagnoseResponse, status_code=202)
def diagnose_incident(
    incident_id: str,
    payload: DiagnoseRequest,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_app_settings),
    enqueue_diagnosis: TaskEnqueue = Depends(get_task_enqueue),
) -> DiagnoseResponse:
    return IncidentService(db, settings, enqueue_diagnosis).trigger_diagnosis(incident_id, payload)


@router.get("/{incident_id}", response_model=IncidentDetailResponse)
def get_incident(
    incident_id: str,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_app_settings),
    enqueue_diagnosis: TaskEnqueue = Depends(get_task_enqueue),
) -> IncidentDetailResponse:
    return IncidentService(db, settings, enqueue_diagnosis).get_detail(incident_id)
