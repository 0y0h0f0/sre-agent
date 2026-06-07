"""Router for evaluation management."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Path, status
from sqlalchemy.orm import Session

from apps.api.dependencies import get_db
from apps.api.schemas.evals import (
    EvalRunDetail,
    EvalRunListResponse,
    EvalRunRequest,
    EvalRunResponse,
    ShadowRunRequest,
    ShadowRunResponse,
)
from apps.api.services.eval_service import EvalService

router = APIRouter(prefix="/api/evals", tags=["evals"])


@router.post("/runs", response_model=EvalRunResponse, status_code=status.HTTP_201_CREATED)
def create_eval_run(
    data: EvalRunRequest,
    db: Session = Depends(get_db),
) -> EvalRunResponse:
    """Trigger an eval run (smoke or full suite)."""
    return EvalService(db).trigger_smoke_eval(data)


@router.get("/runs", response_model=EvalRunListResponse)
def list_eval_runs(
    db: Session = Depends(get_db),
) -> EvalRunListResponse:
    """List recent eval runs."""
    return EvalService(db).list_runs()


@router.get("/runs/{eval_run_id}", response_model=EvalRunDetail)
def get_eval_run(
    eval_run_id: str = Path(..., description="Eval run public ID"),
    db: Session = Depends(get_db),
) -> EvalRunDetail:
    """Get a single eval run by ID."""
    return EvalService(db).get_run(eval_run_id)


@router.post("/shadow", response_model=ShadowRunResponse, status_code=status.HTTP_201_CREATED)
def trigger_shadow(
    data: ShadowRunRequest,
    db: Session = Depends(get_db),
) -> ShadowRunResponse:
    """Trigger a shadow mode run for an incident."""
    return EvalService(db).trigger_shadow(data)
