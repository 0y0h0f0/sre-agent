from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from apps.api.dependencies import get_app_settings, get_db
from apps.api.schemas.runbooks import (
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
# Versions (4.3)
# ---------------------------------------------------------------------------


@router.get("/versions/{document_id}", response_model=list[RunbookVersionItem])
def list_versions(
    document_id: str,
    db: Session = Depends(get_db),
) -> list[RunbookVersionItem]:
    return RunbookService(db).list_versions(document_id)
