"""Router for evaluation management."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, status
from sqlalchemy.orm import Session

from apps.api.dependencies import get_db
from apps.api.schemas.evals import (
    EngineeringMetricsResponse,
    EvalRunDetail,
    EvalRunListResponse,
    EvalRunRequest,
    EvalRunResponse,
    ReplayRunRequest,
    ShadowRunRequest,
    ShadowRunResponse,
)
from apps.api.services.engineering_metrics_service import EngineeringMetricsService
from apps.api.services.eval_service import EvalService

router = APIRouter(prefix="/api/evals", tags=["evals"])
WindowDays = Annotated[int, Query(ge=1, le=365)]


@router.get("/engineering-metrics", response_model=EngineeringMetricsResponse)
def get_engineering_metrics(
    window_days: WindowDays = 30,
    db: Session = Depends(get_db),
) -> EngineeringMetricsResponse:
    """Return project-level engineering metrics from eval and runtime records."""
    return EngineeringMetricsService(db).get_summary(window_days=window_days)


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


@router.post("/replay", response_model=EvalRunResponse, status_code=status.HTTP_201_CREATED)
def trigger_replay(
    data: ReplayRunRequest,
    db: Session = Depends(get_db),
) -> EvalRunResponse:
    """Trigger a safe historical incident replay eval."""
    return EvalService(db).trigger_replay(data)
