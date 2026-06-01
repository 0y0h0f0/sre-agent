from __future__ import annotations

from sqlalchemy.orm import Session

from apps.api.schemas.runbooks import (
    RunbookIngestRequest,
    RunbookIngestResponse,
    RunbookSearchItem,
)
from packages.common.errors import ValidationAppError
from packages.db.repositories.runbooks import RunbookChunkRepository
from packages.rag.ingest import RunbookIngestor
from packages.rag.metadata import RunbookMetadataError
from packages.rag.retriever import RunbookRetriever, RunbookSearchQuery


class RunbookService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.repository = RunbookChunkRepository(db)

    def ingest(self, request: RunbookIngestRequest) -> RunbookIngestResponse:
        try:
            result = RunbookIngestor(self.repository).ingest_path(
                request.path,
                reingest=request.reingest,
            )
        except FileNotFoundError as exc:
            raise ValidationAppError(
                "runbook path does not exist",
                details={"path": request.path},
            ) from exc
        except RunbookMetadataError as exc:
            raise ValidationAppError(str(exc), details={"path": request.path}) from exc
        self.db.commit()
        return RunbookIngestResponse.model_validate(result.model_dump())

    def search(
        self,
        *,
        query: str,
        service: str | None,
        incident_type: str | None,
        top_k: int,
    ) -> list[RunbookSearchItem]:
        results = RunbookRetriever(self.repository).search(
            RunbookSearchQuery(
                query=query,
                service=service,
                incident_type=incident_type,
                top_k=top_k,
            )
        )
        return [RunbookSearchItem.model_validate(result.model_dump()) for result in results]
