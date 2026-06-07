"""Pydantic schemas for incident comments and evidence annotations."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class CommentCreate(BaseModel):
    author: str = Field(..., min_length=1, max_length=128)
    content: str = Field(..., min_length=1, max_length=5000)
    parent_comment_id: str | None = Field(default=None, max_length=64)
    mentioned_users: list[str] = Field(default_factory=list, max_length=20)


class CommentItem(BaseModel):
    comment_id: str
    incident_id: str
    author: str
    content: str
    parent_comment_id: str | None = None
    mentioned_users: list[str] = Field(default_factory=list)
    created_at: datetime | None = None


class CommentListResponse(BaseModel):
    items: list[CommentItem]
    total: int


class AnnotationCreate(BaseModel):
    author: str = Field(..., min_length=1, max_length=128)
    content: str = Field(..., min_length=1, max_length=5000)


class AnnotationItem(BaseModel):
    annotation_id: str
    evidence_id: str
    incident_id: str
    author: str
    content: str
    created_at: datetime | None = None


class AnnotationListResponse(BaseModel):
    items: list[AnnotationItem]
    total: int
