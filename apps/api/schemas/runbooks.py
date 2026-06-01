from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class RunbookIngestRequest(BaseModel):
    path: str = "demo/runbooks"
    reingest: bool = True


class RunbookIngestResponse(BaseModel):
    path: str
    files_scanned: int
    chunks_created: int
    chunks_skipped: int
    chunks_total: int
    errors: list[str] = Field(default_factory=list)


class RunbookSearchItem(BaseModel):
    chunk_id: str
    source_path: str
    title: str
    excerpt: str
    score: float
    metadata: dict[str, Any] = Field(default_factory=dict)
