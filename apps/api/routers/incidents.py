from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from apps.api.dependencies import TaskEnqueue, get_app_settings, get_db, get_task_enqueue
from apps.api.schemas.agent_runs import AgentRunSummary
from apps.api.schemas.common import PaginatedResponse
from apps.api.schemas.audit import AuditLogItem, AuditLogListResponse
from apps.api.schemas.feedback import (
    ActionCorrectionRequest,
    CorrelatedIncident,
    FeedbackListResponse,
    FeedbackResponse,
    NfaMarkRequest,
    NfaMarkResponse,
    RootCauseCorrectionRequest,
)
from apps.api.schemas.incidents import (
    DiagnoseRequest,
    DiagnoseResponse,
    IncidentDetailResponse,
)
from apps.api.services.feedback_service import FeedbackService
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
) -> PaginatedResponse:
    return IncidentService(db, settings).list_incidents(
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
) -> list[AgentRunSummary]:
    return IncidentService(db, settings).list_runs(incident_id)


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
) -> IncidentDetailResponse:
    return IncidentService(db, settings).get_detail(incident_id)


# ---------------------------------------------------------------------------
# Phase 5: Memory & Continuous Learning
# ---------------------------------------------------------------------------


@router.post("/{incident_id}/nfa", response_model=NfaMarkResponse, status_code=201)
def mark_incident_nfa(
    incident_id: str,
    payload: NfaMarkRequest,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_app_settings),
) -> NfaMarkResponse:
    return FeedbackService(db, settings).mark_nfa(incident_id, payload)


@router.patch("/{incident_id}/root-cause", response_model=FeedbackResponse)
def correct_incident_root_cause(
    incident_id: str,
    payload: RootCauseCorrectionRequest,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_app_settings),
) -> FeedbackResponse:
    return FeedbackService(db, settings).correct_root_cause(incident_id, payload)


@router.post("/{incident_id}/actions/{action_id}/feedback", response_model=FeedbackResponse)
def correct_incident_action(
    incident_id: str,
    action_id: str,
    payload: ActionCorrectionRequest,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_app_settings),
) -> FeedbackResponse:
    return FeedbackService(db, settings).correct_action(incident_id, action_id, payload)


@router.get("/{incident_id}/correlated", response_model=list[CorrelatedIncident])
def get_correlated_incidents(
    incident_id: str,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_app_settings),
) -> list[CorrelatedIncident]:
    return FeedbackService(db, settings).get_correlated_incidents(incident_id)


@router.get("/{incident_id}/feedback", response_model=FeedbackListResponse)
def list_incident_feedback(
    incident_id: str,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_app_settings),
) -> FeedbackListResponse:
    items = FeedbackService(db, settings).list_feedback(incident_id)
    return FeedbackListResponse(items=items, total=len(items))


@router.get("/{incident_id}/audit", response_model=AuditLogListResponse)
def list_incident_audit(
    incident_id: str,
    db: Session = Depends(get_db),
) -> AuditLogListResponse:
    from packages.db.repositories.audit_logs import AuditLogRepository

    repo = AuditLogRepository(db)
    items = repo.list_for_incident(incident_id)
    audit_items = [
        AuditLogItem(
            audit_id=a.audit_id,
            incident_id=a.incident_id,
            actor=a.actor,
            action=a.action,
            resource_type=a.resource_type,
            resource_id=a.resource_id,
            details=a.details,
            created_at=a.created_at,
        )
        for a in items
    ]
    return AuditLogListResponse(items=audit_items, total=len(audit_items))
