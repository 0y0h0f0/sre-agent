"""Approval endpoints — list, approve, reject, batch, email token."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from fastapi.responses import RedirectResponse
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
    BatchApprovalRequest,
    RejectRequest,
    TokenApprovalRequest,
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


@router.get("/approvals/{approval_id}", response_model=ApprovalItem)
def get_approval(
    approval_id: str,
    db: Session = Depends(get_db),
    enqueue_resume: ResumeTaskEnqueue = Depends(get_resume_task_enqueue),
) -> ApprovalItem:
    return _service(db, enqueue_resume).get_approval(approval_id)


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


@router.post(
    "/approvals/batch",
    response_model=list[ApprovalDecisionResponse],
)
def batch_decide(
    request: BatchApprovalRequest,
    db: Session = Depends(get_db),
    enqueue_resume: ResumeTaskEnqueue = Depends(get_resume_task_enqueue),
) -> list[ApprovalDecisionResponse]:
    return _service(db, enqueue_resume).batch_decide(request)


@router.post(
    "/approvals/{approval_id}/email-token",
    response_model=dict,
)
def generate_email_token(
    approval_id: str,
    db: Session = Depends(get_db),
    enqueue_resume: ResumeTaskEnqueue = Depends(get_resume_task_enqueue),
) -> dict:
    token = _service(db, enqueue_resume).generate_email_token(approval_id)
    return {"approval_id": approval_id, "email_token": token}


@router.get("/approvals/by-token/{token}")
def get_by_token(
    token: str,
    db: Session = Depends(get_db),
    enqueue_resume: ResumeTaskEnqueue = Depends(get_resume_task_enqueue),
) -> RedirectResponse:
    """Redirect from email token to the frontend approval page."""
    svc = _service(db, enqueue_resume)
    approval = svc.get_approval_by_token(token)
    return RedirectResponse(
        url=f"/approvals/{approval.approval_id}",
        status_code=302,
    )


@router.post(
    "/approvals/by-token/{token}/approve",
    response_model=ApprovalDecisionResponse,
)
def approve_by_token(
    token: str,
    request: TokenApprovalRequest,
    db: Session = Depends(get_db),
    enqueue_resume: ResumeTaskEnqueue = Depends(get_resume_task_enqueue),
) -> ApprovalDecisionResponse:
    return _service(db, enqueue_resume).approve_by_token(token, request)


@router.post(
    "/approvals/by-token/{token}/reject",
    response_model=ApprovalDecisionResponse,
)
def reject_by_token(
    token: str,
    request: TokenApprovalRequest,
    db: Session = Depends(get_db),
    enqueue_resume: ResumeTaskEnqueue = Depends(get_resume_task_enqueue),
) -> ApprovalDecisionResponse:
    return _service(db, enqueue_resume).reject_by_token(token, request)
