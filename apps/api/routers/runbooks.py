from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from apps.api.dependencies import get_db
from apps.api.schemas.runbooks import (
    RunbookIngestRequest,
    RunbookIngestResponse,
    RunbookSearchItem,
)
from apps.api.services.runbook_service import RunbookService

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
