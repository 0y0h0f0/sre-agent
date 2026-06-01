from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from apps.api.schemas.common import Severity


class AlertCreateRequest(BaseModel):
    source: Literal["alertmanager", "mock"]
    fingerprint: str = Field(min_length=1, max_length=255)
    service: str = Field(min_length=1, max_length=128)
    severity: Severity
    alert_name: str = Field(min_length=1, max_length=255)
    starts_at: datetime
    ends_at: datetime | None = None
    labels: dict[str, str] = Field(default_factory=dict)
    annotations: dict[str, str] = Field(default_factory=dict)

    @field_validator("fingerprint", "service", "alert_name")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            msg = "field must not be blank"
            raise ValueError(msg)
        return stripped

    @field_validator("ends_at")
    @classmethod
    def validate_ends_at_after_starts_at(cls, value: datetime | None, info: Any) -> datetime | None:
        if value is not None and "starts_at" in info.data:
            starts_at = info.data["starts_at"]
            if value <= starts_at:
                msg = "ends_at must be after starts_at"
                raise ValueError(msg)
        return value


class AlertCreateResponse(BaseModel):
    incident_id: str
    agent_run_id: str
    celery_task_id: str
    status: str  # "queued" for new incidents; existing status when deduplicated
    deduplicated: bool
