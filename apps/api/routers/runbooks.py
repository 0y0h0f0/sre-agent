from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from apps.api.dependencies import get_app_settings, get_db, require_scope
from apps.api.schemas.runbooks import (
    AmendmentDraftItem,
    AmendmentReviewRequest,
    IncidentDiffRequest,
    IncidentDiffResponse,
    LLMRunbookGenerateRequest,
    LLMRunbookGenerateResponse,
    RunbookDraftGenerateRequest,
    RunbookDraftGenerateResponse,
    RunbookDraftItem,
    RunbookDraftRegenerateRequest,
    RunbookDraftReviewRequest,
    RunbookIngestRequest,
    RunbookIngestResponse,
    RunbookSearchItem,
    RunbookTemplateGenerateRequest,
    RunbookTemplateGenerateResponse,
    RunbookVersionItem,
    WebSearchRequest,
    WebSearchResponse,
)
from apps.api.services.runbook_service import RunbookService
from packages.agent.llm.factory import build_llm
from packages.common.settings import Settings

router = APIRouter(prefix="/api/runbooks", tags=["runbooks"])
TopK = Annotated[int, Query(ge=1, le=20)]
SearchQuery = Annotated[str, Query(alias="q", min_length=1)]


@router.post("/ingest", response_model=RunbookIngestResponse)
def ingest_runbooks(
    payload: RunbookIngestRequest,
    db: Session = Depends(get_db),
) -> RunbookIngestResponse:
    return RunbookService(db).ingest(payload)


@router.get("/search", response_model=list[RunbookSearchItem])
def search_runbooks(
    q: SearchQuery,
    service: str | None = None,
    incident_type: str | None = None,
    top_k: TopK = 5,
    db: Session = Depends(get_db),
) -> list[RunbookSearchItem]:
    return RunbookService(db).search(
        query=q,
        service=service,
        incident_type=incident_type,
        top_k=top_k,
    )


# ---------------------------------------------------------------------------
# Drafts (4.3)
# ---------------------------------------------------------------------------


@router.get("/drafts", response_model=list[RunbookDraftItem])
def list_drafts(
    status: str | None = None,
    service: str | None = None,
    db: Session = Depends(get_db),
) -> list[RunbookDraftItem]:
    return RunbookService(db).list_drafts(status=status, service=service)


@router.get("/drafts/{draft_id}", response_model=RunbookDraftItem)
def get_draft(draft_id: str, db: Session = Depends(get_db)) -> RunbookDraftItem:
    return RunbookService(db).get_draft(draft_id)


@router.post("/drafts/generate", response_model=RunbookDraftGenerateResponse)
def generate_drafts(
    payload: RunbookDraftGenerateRequest,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_app_settings),
) -> RunbookDraftGenerateResponse:
    llm = build_llm(settings)
    return RunbookService(db).generate_drafts(payload, llm)


@router.post("/drafts/{draft_id}/review", response_model=RunbookDraftItem)
def review_draft(
    draft_id: str,
    payload: RunbookDraftReviewRequest,
    db: Session = Depends(get_db),
) -> RunbookDraftItem:
    return RunbookService(db).review_draft(draft_id, payload)


@router.post("/drafts/{draft_id}/regenerate", response_model=RunbookDraftItem)
def regenerate_draft(
    draft_id: str,
    payload: RunbookDraftRegenerateRequest,
    db: Session = Depends(get_db),
) -> RunbookDraftItem:
    return RunbookService(db).regenerate_draft(draft_id, payload)


@router.post("/template", response_model=RunbookTemplateGenerateResponse)
def generate_template(
    payload: RunbookTemplateGenerateRequest,
    db: Session = Depends(get_db),
) -> RunbookTemplateGenerateResponse:
    return RunbookService(db).generate_template_draft(payload)


# ---------------------------------------------------------------------------
# M9: LLM Runbook Draft Generation (PR 9.2)
# ---------------------------------------------------------------------------

_require_runbook_llm = require_scope("runbook:review", "runbook:llm_generate")


@router.post("/llm-generate", response_model=LLMRunbookGenerateResponse)
def llm_generate_runbook(
    payload: LLMRunbookGenerateRequest,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_app_settings),
    _scope: None = Depends(_require_runbook_llm),
) -> LLMRunbookGenerateResponse:
    llm = build_llm(settings)
    return RunbookService(db).llm_generate_draft(payload, llm, settings)


# ---------------------------------------------------------------------------
# M9: Web Search for Runbook Enrichment (PR 9.4)
# ---------------------------------------------------------------------------

_require_web_search_scope = require_scope("runbook:review", "runbook:web_search")


@router.post("/web-search", response_model=WebSearchResponse)
def web_search(
    payload: WebSearchRequest,
    settings: Settings = Depends(get_app_settings),
    _scope: None = Depends(_require_web_search_scope),
) -> WebSearchResponse:
    from packages.rag.runbook_web_context import RunbookWebContextBuilder

    builder = RunbookWebContextBuilder(settings=settings)
    result = builder.build_context(query=payload.query, purpose=payload.purpose)
    return WebSearchResponse(
        status=result.status,
        purpose=result.purpose,
        results=[
            {
                "title": r.title,
                "original_url": r.original_url,
                "final_url": r.final_url,
                "snippet": r.snippet,
                "content_hash": r.content_hash,
                "provider": r.provider,
            }
            for r in result.results
        ],
        query_redacted=result.query_redacted,
        error_message=result.error_message,
    )


# ---------------------------------------------------------------------------
# M9: LLM Incident Diff Analysis (PR 9.3)
# ---------------------------------------------------------------------------

_require_incident_diff = require_scope("runbook:review", "incident:llm_diff")


@router.post("/incident-diff", response_model=IncidentDiffResponse)
def incident_diff(
    payload: IncidentDiffRequest,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_app_settings),
    _scope: None = Depends(_require_incident_diff),
) -> IncidentDiffResponse:
    llm = build_llm(settings)
    return RunbookService(db).llm_incident_diff(payload, llm, settings)


@router.get("/amendments", response_model=list[AmendmentDraftItem])
def list_amendments(
    status: str | None = None,
    service: str | None = None,
    db: Session = Depends(get_db),
) -> list[AmendmentDraftItem]:
    return RunbookService(db).list_amendments(status=status, service=service)


@router.post("/amendments/{amendment_id}/review", response_model=AmendmentDraftItem)
def review_amendment(
    amendment_id: str,
    payload: AmendmentReviewRequest,
    db: Session = Depends(get_db),
) -> AmendmentDraftItem:
    return RunbookService(db).review_amendment(amendment_id, payload)


# ---------------------------------------------------------------------------
# Versions (4.3)
# ---------------------------------------------------------------------------


@router.get("/versions/{document_id}", response_model=list[RunbookVersionItem])
def list_versions(
    document_id: str,
    db: Session = Depends(get_db),
) -> list[RunbookVersionItem]:
    return RunbookService(db).list_versions(document_id)
