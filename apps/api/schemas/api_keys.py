"""Pydantic schemas for API key management."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class ApiKeyCreateRequest(BaseModel):
    description: str = Field(..., min_length=1, max_length=255)
    expires_in_days: int | None = Field(default=None, gt=0)


class ApiKeyCreateResponse(BaseModel):
    key_id: str
    description: str
    raw_key: str
    created_by: str
    expires_at: datetime | None
    created_at: datetime


class ApiKeyListItem(BaseModel):
    key_id: str
    description: str
    created_by: str
    expires_at: datetime | None
    last_used_at: datetime | None
    revoked: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class ApiKeyListResponse(BaseModel):
    items: list[ApiKeyListItem]
    total: int
