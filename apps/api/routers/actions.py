"""Action endpoints — detail and execute."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from apps.api.dependencies import get_db
from apps.api.schemas.actions import (
    ActionDetailResponse,
    ExecuteRequest,
    ExecuteResponse,
)
from apps.api.services.action_service import ActionService

router = APIRouter(prefix="/api", tags=["actions"])


@router.get("/actions/{action_id}", response_model=ActionDetailResponse)
def get_action(
    action_id: str,
    db: Session = Depends(get_db),
) -> ActionDetailResponse:
    return ActionService(db).get_detail(action_id)


@router.post("/actions/{action_id}/execute", response_model=ExecuteResponse)
def execute_action(
    action_id: str,
    request: ExecuteRequest,
    db: Session = Depends(get_db),
) -> ExecuteResponse:
    return ActionService(db).execute(action_id, request)
