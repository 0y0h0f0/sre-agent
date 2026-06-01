"""Approval endpoints — list, approve, reject."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from apps.api.dependencies import (
    ResumeTaskEnqueue,
    get_db,
    get_resume_task_enqueue,
)
from apps.api.schemas.approvals import (
    ApprovalDecisionResponse,
    ApprovalItem,
    ApproveRequest,
    RejectRequest,
)
from apps.api.schemas.common import PaginatedResponse
from apps.api.services.approval_service import ApprovalService

router = APIRouter(prefix="/api", tags=["approvals"])
Page = Annotated[int, Query(ge=1)]
PageSize = Annotated[int, Query(ge=1, le=100)]


def _service(db: Session, enqueue_resume: ResumeTaskEnqueue) -> ApprovalService:
    return ApprovalService(db, enqueue_resume=enqueue_resume)


@router.get("/approvals", response_model=PaginatedResponse)
def list_approvals(
    status: str | None = None,
    incident_id: str | None = None,
    service: str | None = None,
    risk_level: str | None = None,
    page: Page = 1,
    page_size: PageSize = 20,
    db: Session = Depends(get_db),
    enqueue_resume: ResumeTaskEnqueue = Depends(get_resume_task_enqueue),
) -> PaginatedResponse:
    return _service(db, enqueue_resume).list_approvals(
        status=status,
        incident_id=incident_id,
        service=service,
        risk_level=risk_level,
        page=page,
        page_size=page_size,
    )


@router.get("/incidents/{incident_id}/approvals", response_model=list[ApprovalItem])
def list_incident_approvals(
    incident_id: str,
    db: Session = Depends(get_db),
    enqueue_resume: ResumeTaskEnqueue = Depends(get_resume_task_enqueue),
) -> list[ApprovalItem]:
    return _service(db, enqueue_resume).list_for_incident(incident_id)


@router.post(
    "/approvals/{approval_id}/approve",
    response_model=ApprovalDecisionResponse,
)
def approve_action(
    approval_id: str,
    request: ApproveRequest,
    db: Session = Depends(get_db),
    enqueue_resume: ResumeTaskEnqueue = Depends(get_resume_task_enqueue),
) -> ApprovalDecisionResponse:
    return _service(db, enqueue_resume).approve(approval_id, request)


@router.post(
    "/approvals/{approval_id}/reject",
    response_model=ApprovalDecisionResponse,
)
def reject_action(
    approval_id: str,
    request: RejectRequest,
    db: Session = Depends(get_db),
    enqueue_resume: ResumeTaskEnqueue = Depends(get_resume_task_enqueue),
) -> ApprovalDecisionResponse:
    return _service(db, enqueue_resume).reject(approval_id, request)
