from __future__ import annotations

import hashlib

from sqlalchemy.orm import Session

from apps.api.schemas.runbooks import (
    RunbookDraftGenerateRequest,
    RunbookDraftGenerateResponse,
    RunbookDraftItem,
    RunbookDraftReviewRequest,
    RunbookIngestRequest,
    RunbookIngestResponse,
    RunbookSearchItem,
    RunbookVersionItem,
)
from packages.agent.llm.base import LLMProvider
from packages.common.errors import NotFoundError, ValidationAppError
from packages.db.models import RunbookDraft
from packages.db.repositories.incidents import IncidentRepository
from packages.db.repositories.runbook_drafts import RunbookDraftRepository
from packages.db.repositories.runbook_versions import RunbookVersionRepository
from packages.db.repositories.runbooks import RunbookChunkRepository
from packages.rag.ingest import RunbookIngestor
from packages.rag.metadata import RunbookMetadataError
from packages.rag.retriever import RunbookRetriever, RunbookSearchQuery
from packages.rag.runbook_generator import RunbookGenerator
from packages.rag.template_extractor import TemplateExtractor


class RunbookService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.repository = RunbookChunkRepository(db)
        self._draft_repo = RunbookDraftRepository(db)
        self._version_repo = RunbookVersionRepository(db)

    # ------------------------------------------------------------------
    # ingest / search (existing)
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # drafts (4.3)
    # ------------------------------------------------------------------

    def list_drafts(
        self, *, status: str | None = None, service: str | None = None
    ) -> list[RunbookDraftItem]:
        drafts = self._draft_repo.list_all(status=status, service=service)
        return [_draft_to_item(d) for d in drafts]

    def get_draft(self, draft_id: str) -> RunbookDraftItem:
        draft = self._draft_repo.get_by_draft_id(draft_id)
        if draft is None:
            raise NotFoundError("draft", draft_id)
        return _draft_to_item(draft)

    def generate_drafts(
        self,
        request: RunbookDraftGenerateRequest,
        llm: LLMProvider,
    ) -> RunbookDraftGenerateResponse:
        extractor = TemplateExtractor(IncidentRepository(self.db))
        generator = RunbookGenerator(llm, self._draft_repo, extractor)
        draft_ids = generator.generate_all(
            min_incident_count=request.min_incident_count,
            fingerprint=request.fingerprint,
        )
        self.db.commit()
        return RunbookDraftGenerateResponse(
            drafts_created=len(draft_ids),
            draft_ids=draft_ids,
        )

    def review_draft(self, draft_id: str, request: RunbookDraftReviewRequest) -> RunbookDraftItem:
        if request.status not in ("published", "rejected"):
            raise ValidationAppError(
                "status must be 'published' or 'rejected'",
                details={"status": request.status},
            )
        draft = self._draft_repo.update_status(
            draft_id,
            request.status,
            reviewer=request.reviewer,
            comment=request.comment,
        )
        if draft is None:
            raise NotFoundError("draft", draft_id)

        if request.status == "published":
            self._version_repo.create(
                document_id=draft.draft_id,
                source_path=f"drafts/{draft.draft_id}.md",
                content_hash=hashlib.sha256(draft.content.encode()).hexdigest(),
                change_reason="published_from_draft",
                related_draft_id=draft.draft_id,
                created_by=request.reviewer,
            )

        self.db.commit()
        return _draft_to_item(draft)

    # ------------------------------------------------------------------
    # versions (4.3)
    # ------------------------------------------------------------------

    def list_versions(self, document_id: str) -> list[RunbookVersionItem]:
        versions = self._version_repo.list_versions(document_id)
        return [
            RunbookVersionItem(
                version_id=v.version_id,
                document_id=v.document_id,
                version_number=v.version_number,
                source_path=v.source_path,
                content_hash=v.content_hash,
                change_reason=v.change_reason,
                related_incident_id=v.related_incident_id,
                related_draft_id=v.related_draft_id,
                diff_from_previous=v.diff_from_previous,
                created_by=v.created_by,
                created_at=v.created_at.isoformat(),
            )
            for v in versions
        ]


def _draft_to_item(draft: RunbookDraft) -> RunbookDraftItem:
    return RunbookDraftItem(
        draft_id=draft.draft_id,
        fingerprint=draft.fingerprint,
        incident_ids=draft.incident_ids or [],
        service=draft.service,
        incident_type=draft.incident_type,
        title=draft.title,
        content=draft.content,
        status=draft.status,
        reviewer=draft.reviewer,
        review_comment=draft.review_comment,
        source_chunk_ids=draft.source_chunk_ids,
        llm_model=draft.llm_model,
        created_at=draft.created_at.isoformat(),
        updated_at=draft.updated_at.isoformat(),
    )
