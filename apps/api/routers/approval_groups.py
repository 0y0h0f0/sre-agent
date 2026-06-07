"""REST endpoints for approval group management."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from apps.api.dependencies import get_db
from apps.api.schemas.approval_groups import (
    ApprovalGroupCreate,
    ApprovalGroupItem,
    ApprovalGroupListResponse,
    ApprovalGroupUpdate,
)
from apps.api.services.approval_group_service import ApprovalGroupService

router = APIRouter(prefix="/api", tags=["approval_groups"])


def _service(db: Session = Depends(get_db)) -> ApprovalGroupService:
    return ApprovalGroupService(db)


@router.post("/approval-groups", response_model=ApprovalGroupItem, status_code=201)
def create_group(
    body: ApprovalGroupCreate,
    svc: ApprovalGroupService = Depends(_service),
) -> ApprovalGroupItem:
    return svc.create(body)


@router.get("/approval-groups", response_model=ApprovalGroupListResponse)
def list_groups(
    svc: ApprovalGroupService = Depends(_service),
) -> ApprovalGroupListResponse:
    return svc.list_all()


@router.get("/approval-groups/{group_id}", response_model=ApprovalGroupItem)
def get_group(
    group_id: str,
    svc: ApprovalGroupService = Depends(_service),
) -> ApprovalGroupItem:
    return svc.get(group_id)


@router.patch("/approval-groups/{group_id}", response_model=ApprovalGroupItem)
def update_group(
    group_id: str,
    body: ApprovalGroupUpdate,
    svc: ApprovalGroupService = Depends(_service),
) -> ApprovalGroupItem:
    return svc.update(group_id, body)


@router.delete("/approval-groups/{group_id}", status_code=204)
def delete_group(
    group_id: str,
    svc: ApprovalGroupService = Depends(_service),
) -> None:
    svc.delete(group_id)
