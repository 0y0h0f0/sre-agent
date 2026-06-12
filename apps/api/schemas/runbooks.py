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


# ---------------------------------------------------------------------------
# Draft schemas (4.3)
# ---------------------------------------------------------------------------


class RunbookDraftItem(BaseModel):
    draft_id: str
    fingerprint: str
    incident_ids: list[str]
    service: str
    incident_type: str
    title: str
    content: str
    status: str
    draft_type: str = "incident_cluster"
    source: str = "llm"
    discovery_run_id: str | None = None
    parent_draft_id: str | None = None
    reviewer: str | None = None
    review_comment: str | None = None
    source_chunk_ids: list[str] | None = None
    llm_model: str | None = None
    created_at: str
    updated_at: str


class RunbookDraftGenerateRequest(BaseModel):
    min_incident_count: int = Field(default=3, ge=2)
    fingerprint: str | None = None


class RunbookDraftGenerateResponse(BaseModel):
    drafts_created: int
    draft_ids: list[str]


class RunbookDraftReviewRequest(BaseModel):
    status: str  # "published" or "rejected"
    reviewer: str = Field(min_length=1)
    comment: str | None = None


class RunbookDraftRegenerateRequest(BaseModel):
    reviewer: str = Field(min_length=1)
    comment: str | None = None


class RunbookTemplateGenerateRequest(BaseModel):
    service_name: str = Field(min_length=1)
    incident_type: str = Field(min_length=1)
    title: str | None = None
    severity: str = "P2"
    owner: str = "agent"
    discovery_run_id: str | None = None


class RunbookTemplateGenerateResponse(BaseModel):
    draft_id: str
    title: str
    incident_type: str
    service_name: str


# ---------------------------------------------------------------------------
# Version schemas (4.3)
# ---------------------------------------------------------------------------


class RunbookVersionItem(BaseModel):
    version_id: str
    document_id: str
    version_number: int
    source_path: str
    content_hash: str
    change_reason: str
    related_incident_id: str | None = None
    related_draft_id: str | None = None
    diff_from_previous: str | None = None
    created_by: str
    created_at: str
