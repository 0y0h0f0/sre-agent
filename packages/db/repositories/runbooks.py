"""Repository for Runbook RAG chunks."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from packages.db.models import RunbookChunk


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
        On SQLite (no tsvector), returns an empty list gracefully.
        Metadata filtering is done in Python for cross-dialect compatibility.
        """
        # Safety: validate tsquery before embedding in SQL.
        # build_tsquery restricts to [a-z0-9_:*& ] — rejecting any input
        # that contains characters outside this allowlist.
        _require_safe_tsquery(tsquery)

        try:
            rank = func.ts_rank_cd(
                RunbookChunk.tsv_content,
                func.to_tsquery(text("'english'"), text(f"'{tsquery}'")),
            ).label("rank")
        except Exception:
            import logging
            logging.getLogger(__name__).warning(
                "runbook full-text search query failed", exc_info=True,
            )
            return []

        stmt = (
            select(RunbookChunk, rank)
            .where(
                RunbookChunk.tsv_content.op("@@")(
                    func.to_tsquery(text("'english'"), text(f"'{tsquery}'"))
                )
            )
            .order_by(text("rank DESC"))
            .limit(50)
        )
        try:
            rows = self.db.execute(stmt).all()
        except Exception:
            import logging
            logging.getLogger(__name__).warning(
                "runbook full-text search query failed", exc_info=True,
            )
            return []

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


_TSQUERY_SAFE_RE = __import__("re").compile(r"^[a-z0-9_:*& ]+$")


def _require_safe_tsquery(tsquery: str) -> None:
    if not _TSQUERY_SAFE_RE.match(tsquery):
        msg = f"unsafe tsquery rejected: {tsquery[:80]}"
        raise ValueError(msg)
