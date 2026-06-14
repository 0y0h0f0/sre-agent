from __future__ import annotations

from typing import Any

from pydantic import AliasChoices, BaseModel, ConfigDict, Field


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


# ---------------------------------------------------------------------------
# M9 LLM Runbook Generation schemas (PR 9.2)
# ---------------------------------------------------------------------------


class LLMRunbookGenerateRequest(BaseModel):
    """Request to generate a runbook draft via LLM.

    All context fields are optional — the LLM can work with just service
    and incident_type, but more context produces better results.
    """

    service: str = Field(min_length=1, max_length=128)
    incident_type: str = Field(min_length=1, max_length=64)
    runbook_context: list[str] | None = None
    evidence_summary: str | None = None
    template_draft: str | None = None
    capability_gaps: list[str] | None = None
    effective_config: dict[str, object] | None = None
    evidence_ids: list[str] | None = None


class LLMRunbookGenerateResponse(BaseModel):
    """Response from an LLM runbook draft generation attempt."""

    status: str  # "generated" | "disabled" | "blocked" | "degraded"
    draft_id: str | None = None
    draft_status: str | None = None
    draft_type: str | None = None
    action_classification_summary: dict[str, object] | None = None
    error_message: str | None = None


# ---------------------------------------------------------------------------
# M9 LLM Incident Diff schemas (PR 9.3)
# ---------------------------------------------------------------------------


class IncidentDiffRequest(BaseModel):
    """Request to analyze differences between an incident and an approved runbook."""

    incident_id: str | None = None
    approved_runbook_version_id: str | None = None
    service: str = Field(min_length=1, max_length=128)
    fault_type: str = Field(min_length=1, max_length=128)
    approved_runbook: str = Field(min_length=1)
    diagnosis_report: str | None = None
    operator_feedback: str | None = None
    action_execution_results: list[dict[str, object]] | None = None
    linked_approved_runbook_version: str | None = None
    evidence_refs: list[str] | None = None


class AmendmentProposalItem(BaseModel):
    """A single amendment proposal from incident diff analysis."""

    amendment_type: str
    rationale: str
    proposed_content: str
    evidence_refs: list[str] = Field(default_factory=list)
    confidence: str = "low"
    proposal_kind: str = "low_confidence_note"
    can_apply: bool = False


class IncidentDiffResponse(BaseModel):
    """Response from an incident diff analysis attempt."""

    status: str
    # "generated" | "disabled" | "blocked" | "degraded" | "skipped_insufficient_evidence"
    proposals: list[AmendmentProposalItem] = Field(default_factory=list)
    amendment_ids: list[str] = Field(default_factory=list)
    error_message: str | None = None


class AmendmentReviewRequest(BaseModel):
    """Review an M9 amendment draft."""

    model_config = ConfigDict(populate_by_name=True)

    status: str = Field(min_length=1)
    # "approved" | "rejected" | "applied" | "superseded"
    reviewer: str = Field(default="operator", min_length=1)
    comment: str | None = Field(
        default=None,
        validation_alias=AliasChoices("comment", "reviewer_notes"),
    )
    applied_to_draft_id: str | None = None
    applied_to_runbook_version_id: str | None = None
    superseded_by_amendment_id: str | None = None


class AmendmentDraftItem(BaseModel):
    """Amendment draft in list/detail views."""

    amendment_id: str
    service: str
    fault_type: str
    amendment_type: str
    proposed_content: str
    rationale: str
    status: str
    evidence_incident_ids: list[str] = Field(default_factory=list)
    confidence: str = "low"
    proposal_kind: str = "low_confidence_note"
    source: str = "runbook_feedback"
    related_incident_id: str | None = None
    runbook_version_id: str | None = None
    reviewer: str | None = None
    review_comment: str | None = None
    approved_by: str | None = None
    approved_at: str | None = None
    applied_to_draft_id: str | None = None
    applied_to_runbook_version_id: str | None = None
    applied_at: str | None = None
    superseded_by_amendment_id: str | None = None
    created_at: str
    updated_at: str


# ---------------------------------------------------------------------------
# M9 Web Search schemas (PR 9.4)
# ---------------------------------------------------------------------------


class WebSearchRequest(BaseModel):
    """Request to search the web for runbook enrichment context.

    Results are evidence for draft review only — never auto-published.
    """

    query: str = Field(min_length=1, max_length=500)
    purpose: str = "draft_enrichment"


class WebSearchResultItem(BaseModel):
    """A single web search result with traceability metadata."""

    title: str
    original_url: str
    final_url: str
    snippet: str
    content_hash: str
    provider: str


class WebSearchResponse(BaseModel):
    """Response from a web search for runbook enrichment."""

    status: str  # "ok" | "disabled" | "degraded" | "blocked"
    purpose: str = "draft_enrichment"
    results: list[WebSearchResultItem] = Field(default_factory=list)
    query_redacted: str = ""
    error_message: str | None = None
