from __future__ import annotations

import hashlib
import logging

from sqlalchemy.orm import Session

from apps.api.schemas.runbooks import (
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
)
from packages.agent.llm.base import LLMProvider
from packages.common.errors import NotFoundError, ValidationAppError
from packages.common.ids import new_id
from packages.common.settings import Settings
from packages.db.models import RunbookDraft
from packages.db.repositories.incidents import IncidentRepository
from packages.db.repositories.runbook_drafts import RunbookDraftRepository
from packages.db.repositories.runbook_versions import RunbookVersionRepository
from packages.db.repositories.runbooks import RunbookChunkRepository
from packages.rag.embedding_factory import build_embedding_provider
from packages.rag.ingest import RunbookIngestor
from packages.rag.llm_runbook_generator import LLMRunbookGenerator
from packages.rag.metadata import RunbookMetadataError, parse_runbook_markdown
from packages.rag.retriever import RunbookRetriever, RunbookSearchQuery
from packages.rag.runbook_action_classifier import RunbookActionClassifier
from packages.rag.runbook_generator import RunbookGenerator
from packages.rag.runbook_prompt_builder import RunbookPromptBuilder
from packages.rag.splitter import split_markdown_document
from packages.rag.template_extractor import TemplateExtractor

logger = logging.getLogger(__name__)


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
            self._ingest_draft_chunks(draft)

        self.db.commit()
        return _draft_to_item(draft)

    def regenerate_draft(
        self, draft_id: str, request: RunbookDraftRegenerateRequest
    ) -> RunbookDraftItem:
        """Regenerate a draft — creates a NEW pending draft, never overwrites the original.

        The new draft inherits service/incident_type/fingerprint/content from the
        original and sets parent_draft_id for audit trail.
        """
        original = self._draft_repo.get_by_draft_id(draft_id)
        if original is None:
            raise NotFoundError("draft", draft_id)

        new_draft = self._draft_repo.create(
            fingerprint=original.fingerprint,
            incident_ids=list(original.incident_ids or []),
            service=original.service,
            incident_type=original.incident_type,
            title=f"{original.title} (Regenerated)",
            content=original.content,
            front_matter=dict(original.front_matter or {}),
            source_chunk_ids=list(original.source_chunk_ids) if original.source_chunk_ids else None,
            draft_type=getattr(original, "draft_type", "incident_cluster"),
            source="regenerated",
            discovery_run_id=getattr(original, "discovery_run_id", None),
            parent_draft_id=original.draft_id,
        )
        self.db.commit()
        return _draft_to_item(new_draft)

    def generate_template_draft(
        self, request: RunbookTemplateGenerateRequest
    ) -> RunbookTemplateGenerateResponse:
        """Generate a runbook draft deterministically from the template engine."""
        from hashlib import sha256

        from packages.discovery.runbook_template_engine import (
            RunbookTemplateContext,
            RunbookTemplateEngine,
        )

        engine = RunbookTemplateEngine()
        context = RunbookTemplateContext(
            service_name=request.service_name,
            incident_type=request.incident_type,
            title=request.title or f"{request.incident_type.replace('_', ' ').title()} Runbook",
            severity=request.severity,
            owner=request.owner,
        )
        content = engine.render(context)

        fingerprint = sha256(
            f"template:{request.service_name}:{request.incident_type}".encode()
        ).hexdigest()[:16]

        draft = self._draft_repo.create(
            fingerprint=fingerprint,
            incident_ids=[],
            service=request.service_name,
            incident_type=request.incident_type,
            title=context.title,
            content=content,
            front_matter={
                "service": request.service_name,
                "incident_type": request.incident_type,
                "severity": request.severity,
                "owner": request.owner,
                "updated_at": context.today,
            },
            draft_type="template",
            source="template_engine",
            discovery_run_id=request.discovery_run_id,
        )
        self.db.commit()
        return RunbookTemplateGenerateResponse(
            draft_id=draft.draft_id,
            title=draft.title,
            incident_type=draft.incident_type,
            service_name=draft.service,
        )

    # ------------------------------------------------------------------
    # M9: LLM Runbook Draft Generation (PR 9.2)
    # ------------------------------------------------------------------

    def llm_generate_draft(
        self,
        request: LLMRunbookGenerateRequest,
        llm: LLMProvider,
        settings: Settings,
    ) -> LLMRunbookGenerateResponse:
        """Generate a runbook draft via LLM with full safety controls.

        The LLM can only produce a RunbookDraft(status=pending_review).
        It never auto-approves, auto-publishes, or modifies approved runbooks.
        """
        generator = LLMRunbookGenerator(
            settings=settings,
            llm=llm,
            classifier=RunbookActionClassifier(),
            prompt_builder=RunbookPromptBuilder(),
        )

        result = generator.generate(
            service=request.service,
            incident_type=request.incident_type,
            runbook_context=request.runbook_context,
            evidence_summary=request.evidence_summary,
            template_draft=request.template_draft,
            capability_gaps=request.capability_gaps,
            effective_config=request.effective_config if request.effective_config else None,
            evidence_ids=request.evidence_ids,
        )

        if result.status != "generated":
            return LLMRunbookGenerateResponse(
                status=result.status,
                error_message=result.error_message,
            )

        # Persist the draft as pending_review.
        fingerprint = hashlib.sha256(
            f"llm:{request.service}:{request.incident_type}".encode()
        ).hexdigest()[:16]

        draft = self._draft_repo.create(
            fingerprint=fingerprint,
            incident_ids=[],
            service=request.service,
            incident_type=request.incident_type,
            title=f"{request.incident_type.replace('_', ' ').title()} Runbook (LLM)",
            content=result.content or "",
            front_matter={
                "service": request.service,
                "incident_type": request.incident_type,
                "severity": "P2",
                "owner": "llm-generated",
            },
            draft_type="llm_generated",
            source="llm",
            llm_model=(
                result.prompt_metadata.get("model_provider")
                if result.prompt_metadata
                else None
            ),
        )

        # Override status to pending_review (the repo's create() defaults to "draft").
        draft.status = "pending_review"
        self.db.commit()

        return LLMRunbookGenerateResponse(
            status="generated",
            draft_id=draft.draft_id,
            draft_status="pending_review",
            draft_type="llm_generated",
            action_classification_summary=result.action_classification_summary or {},
        )

    def _ingest_draft_chunks(self, draft: RunbookDraft) -> None:
        """Ingest an approved draft's content into runbook_chunks.

        Embedding failures are non-fatal: if the embedding provider is unavailable,
        chunks are stored with empty embeddings (keyword-only search still works).
        """
        try:
            document = parse_runbook_markdown(
                draft.content,
                source_path=f"drafts/{draft.draft_id}.md",
            )
        except RunbookMetadataError as exc:
            logger.warning(
                "Draft %s content could not be parsed as runbook: %s",
                draft.draft_id,
                exc,
            )
            return

        try:
            from packages.common.settings import get_settings
            embedding_provider = build_embedding_provider(get_settings())
        except Exception:
            logger.warning(
                "Embedding provider unavailable for draft %s — storing chunks without embeddings",
                draft.draft_id,
            )
            embedding_provider = None

        chunk_drafts = split_markdown_document(document)
        for cd in chunk_drafts:
            if self.repository.get_by_content_hash(cd.content_hash) is not None:
                continue
            embedding: list[float] = []
            embedding_model = "none"
            if embedding_provider is not None:
                try:
                    embedding = embedding_provider.embed_text(f"{cd.title}\n{cd.content}")
                    embedding_model = embedding_provider.model_name
                except Exception:
                    logger.warning(
                        "Embedding failed for chunk '%s' in draft %s — storing without embedding",
                        cd.title,
                        draft.draft_id,
                    )
            chunk = self.repository.create_chunk(
                chunk_id=new_id("chk_"),
                document_id=cd.document_id,
                source_path=cd.source_path,
                title=cd.title,
                content=cd.content,
                content_hash=cd.content_hash,
                embedding=embedding,
                embedding_model=embedding_model,
                metadata=dict(cd.metadata),
            )
            chunk.language = document.metadata.language

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
        draft_type=getattr(draft, "draft_type", "incident_cluster"),
        source=getattr(draft, "source", "llm"),
        discovery_run_id=getattr(draft, "discovery_run_id", None),
        parent_draft_id=getattr(draft, "parent_draft_id", None),
        reviewer=draft.reviewer,
        review_comment=draft.review_comment,
        source_chunk_ids=draft.source_chunk_ids,
        llm_model=draft.llm_model,
        created_at=draft.created_at.isoformat(),
        updated_at=draft.updated_at.isoformat(),
    )
