"""Pydantic schemas for approval groups."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class ApprovalGroupCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    service_pattern: str = Field(..., min_length=1, max_length=255)
    members: list[str] = Field(default_factory=list, max_length=100)
    is_default: bool = False


class ApprovalGroupUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    service_pattern: str | None = Field(default=None, min_length=1, max_length=255)
    members: list[str] | None = Field(default=None, max_length=100)
    is_default: bool | None = None


class ApprovalGroupItem(BaseModel):
    group_id: str
    name: str
    service_pattern: str
    members: list[str] = Field(default_factory=list)
    is_default: bool
    created_at: datetime | None = None
    updated_at: datetime | None = None


class ApprovalGroupListResponse(BaseModel):
    items: list[ApprovalGroupItem]
    total: int
