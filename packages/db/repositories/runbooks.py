"""Repository for Runbook RAG chunks."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy import func, select
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
