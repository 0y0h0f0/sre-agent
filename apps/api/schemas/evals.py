"""Pydantic schemas for evaluation API."""

from __future__ import annotations

from datetime import datetime
from typing import Any

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


class CompareRequest(BaseModel):
    run_id_a: str
    run_id_b: str


class CompareResponse(BaseModel):
    metrics_diff: dict[str, Any]
