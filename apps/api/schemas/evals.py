"""Pydantic schemas for evaluation API."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class EvalRunRequest(BaseModel):
    suite: str = Field(default="smoke", pattern="^(smoke|full)$")
    model: str | None = None
    prompt_version: str = Field(default="v1")


class EvalRunResponse(BaseModel):
    eval_run_id: str
    status: str
    created_at: datetime


class EvalRunDetail(BaseModel):
    eval_run_id: str
    status: str
    suite: str
    model_name: str
    prompt_version: str
    metrics: dict[str, Any]
    started_at: datetime | None
    finished_at: datetime | None
    git_commit: str
    created_at: datetime

    model_config = {"from_attributes": True}


class EvalRunListResponse(BaseModel):
    items: list[EvalRunDetail]
    total: int


class ShadowRunRequest(BaseModel):
    incident_id: str
    shadow_model: str = Field(default="fake-diagnosis-model")
    shadow_prompt_version: str = Field(default="v1")


class ShadowRunResponse(BaseModel):
    eval_run_id: str
    status: str


class ReplayRunRequest(BaseModel):
    limit: int = Field(default=20, ge=1, le=100)
    service: str | None = Field(default=None, max_length=128)
    incident_ids: list[str] = Field(default_factory=list, max_length=100)
    model: str | None = None
    prompt_version: str = Field(default="v1")


class CompareRequest(BaseModel):
    run_id_a: str
    run_id_b: str


class CompareResponse(BaseModel):
    metrics_diff: dict[str, Any]


class EngineeringMetric(BaseModel):
    key: str
    category: str
    label: str
    value: Any | None = None
    unit: str | None = None
    target: str | None = None
    status: Literal["pass", "fail", "warn", "unknown"] = "unknown"
    score: float | None = None
    weight: float = 1.0
    source: str
    description: str
    reproduction: list[str] = Field(default_factory=list)


class EngineeringCategoryScore(BaseModel):
    category: str
    weight: float
    score: float | None = None
    status: Literal["pass", "fail", "warn", "unknown"] = "unknown"
    metric_count: int
    scored_metric_count: int
    unknown_metric_count: int
    fail_count: int
    warn_count: int


class EngineeringScorecard(BaseModel):
    overall_score: float | None = None
    gate_status: Literal["pass", "fail", "warn", "unknown"] = "unknown"
    completeness_rate: float
    metric_count: int
    scored_metric_count: int
    unknown_metric_count: int
    pass_count: int
    warn_count: int
    fail_count: int
    score_model: str
    category_scores: list[EngineeringCategoryScore]
    top_risks: list[str] = Field(default_factory=list)
    reproduction: list[str] = Field(default_factory=list)


class EngineeringMetricsResponse(BaseModel):
    generated_at: datetime
    window_days: int
    window_started_at: datetime
    latest_smoke_eval_run_id: str | None
    summary: dict[str, Any]
    scorecard: EngineeringScorecard
    metrics: list[EngineeringMetric]
