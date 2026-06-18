"""Repository for Runbook RAG chunks."""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from typing import Any

from sqlalchemy import func, literal, select, text
from sqlalchemy.orm import Session

from packages.db.models import RunbookChunk

RUNBOOK_EMBEDDING_DIMENSION = 512
logger = logging.getLogger(__name__)


class RunbookChunkRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def create_chunk(
        self,
        *,
        chunk_id: str,
        document_id: str,
        source_path: str,
        title: str,
        content: str,
        content_hash: str,
        embedding: list[float],
        embedding_model: str,
        metadata: dict[str, Any],
    ) -> RunbookChunk:
        if len(embedding) != RUNBOOK_EMBEDDING_DIMENSION:
            raise ValueError(
                "runbook chunk embedding dimension "
                f"{len(embedding)} != {RUNBOOK_EMBEDDING_DIMENSION}"
            )
        chunk = RunbookChunk(
            chunk_id=chunk_id,
            document_id=document_id,
            source_path=source_path,
            title=title,
            content=content,
            content_hash=content_hash,
            embedding=embedding,
            embedding_model=embedding_model,
            metadata_json=metadata,
        )
        self.db.add(chunk)
        return chunk

    def get_by_content_hash(self, content_hash: str) -> RunbookChunk | None:
        stmt = select(RunbookChunk).where(RunbookChunk.content_hash == content_hash)
        return self.db.scalar(stmt)

    def document_has_chunks(self, document_id: str) -> bool:
        stmt = select(RunbookChunk.id).where(RunbookChunk.document_id == document_id).limit(1)
        return self.db.scalar(stmt) is not None

    def count_chunks(self) -> int:
        self.db.flush()
        return int(self.db.scalar(select(func.count()).select_from(RunbookChunk)) or 0)

    def list_chunks(self) -> Sequence[RunbookChunk]:
        stmt = select(RunbookChunk).order_by(RunbookChunk.created_at.asc(), RunbookChunk.id.asc())
        return self.db.scalars(stmt).all()

    def search_bm25(
        self,
        tsquery: str,
        *,
        service: str | None = None,
        incident_type: str | None = None,
    ) -> list[tuple[RunbookChunk, float]]:
        """Full-text search using PostgreSQL tsvector + ts_rank_cd.

        Accepts a pre-sanitized tsquery string (from ``build_tsquery``).
        On SQLite (no tsvector), uses a deterministic lexical fallback.
        Metadata filtering is done in Python for cross-dialect compatibility.
        """
        # Safety: validate tsquery before embedding in SQL.
        # build_tsquery restricts to [a-z0-9_:*& ] — rejecting any input
        # that contains characters outside this allowlist.
        _require_safe_tsquery(tsquery)

        if _dialect_name(self.db) != "postgresql":
            return _search_lexical_fallback(
                self.list_chunks(),
                tsquery,
                service=service,
                incident_type=incident_type,
            )

        tsquery_expr = func.to_tsquery(literal("english"), literal(tsquery))
        rank = func.ts_rank_cd(RunbookChunk.tsv_content, tsquery_expr).label("rank")

        stmt = (
            select(RunbookChunk, rank)
            .where(RunbookChunk.tsv_content.op("@@")(tsquery_expr))
            .order_by(text("rank DESC"))
            .limit(50)
        )
        try:
            rows = self.db.execute(stmt).all()
        except Exception:
            logger.warning(
                "runbook full-text search query failed; falling back to lexical search: %s",
                tsquery,
            )
            return _search_lexical_fallback(
                self.list_chunks(),
                tsquery,
                service=service,
                incident_type=incident_type,
            )

        results: list[tuple[RunbookChunk, float]] = []
        for row in rows:
            chunk = row[0]
            score = float(row[1])
            meta = chunk.metadata_json or {}
            if service and meta.get("service", "").lower() != service.lower():
                continue
            if incident_type and meta.get("incident_type") != incident_type:
                continue
            results.append((chunk, score))
        return results


def degraded_runbook_embedding() -> list[float]:
    """Return a deterministic placeholder vector for keyword-only fallback."""
    return [0.0] * RUNBOOK_EMBEDDING_DIMENSION


_TSQUERY_SAFE_RE = re.compile(r"^[a-z0-9_:*& ]+$")
_TERM_RE = re.compile(r"[a-z0-9_]+")


def _require_safe_tsquery(tsquery: str) -> None:
    if not _TSQUERY_SAFE_RE.match(tsquery):
        msg = f"unsafe tsquery rejected: {tsquery[:80]}"
        raise ValueError(msg)


def _dialect_name(db: Session) -> str:
    bind = db.get_bind()
    return getattr(getattr(bind, "dialect", None), "name", "")


def _search_lexical_fallback(
    chunks: Sequence[RunbookChunk],
    tsquery: str,
    *,
    service: str | None,
    incident_type: str | None,
) -> list[tuple[RunbookChunk, float]]:
    terms = _tsquery_terms(tsquery)
    if not terms:
        return []

    scored: list[tuple[RunbookChunk, float]] = []
    for chunk in chunks:
        metadata = chunk.metadata_json or {}
        if service and metadata.get("service", "").lower() != service.lower():
            continue
        metadata_incident_type = (metadata.get("incident_type") or "").lower()
        if incident_type and metadata_incident_type != incident_type.lower():
            continue

        score = _lexical_rank(chunk, terms)
        if score > 0.0:
            scored.append((chunk, score))

    return sorted(scored, key=lambda item: (-item[1], item[0].chunk_id))[:50]


def _tsquery_terms(tsquery: str) -> list[str]:
    terms: list[str] = []
    for term in _TERM_RE.findall(tsquery.lower()):
        if term not in {"and", "or"} and term not in terms:
            terms.append(term)
    return terms


def _lexical_rank(chunk: RunbookChunk, terms: list[str]) -> float:
    title_tokens = _TERM_RE.findall((chunk.title or "").lower())
    content_tokens = _TERM_RE.findall((chunk.content or "").lower())
    if not title_tokens and not content_tokens:
        return 0.0

    weighted_hits = 0
    matched_terms = 0
    for term in terms:
        title_hits = sum(
            1 for token in title_tokens if token == term or token.startswith(term)
        )
        content_hits = sum(
            1 for token in content_tokens if token == term or token.startswith(term)
        )
        if title_hits or content_hits:
            matched_terms += 1
            weighted_hits += title_hits * 2 + content_hits

    coverage = matched_terms / len(terms)
    frequency = min(1.0, weighted_hits / max(1, len(terms) * 3))
    return min(1.0, 0.7 * coverage + 0.3 * frequency)
