"""Multi-level memory store (L0-L3) backed by PostgreSQL and pgvector."""

from __future__ import annotations

from sqlalchemy import and_, or_, true
from sqlalchemy import select as sa_select
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from packages.common.ids import new_id
from packages.common.time import utc_now
from packages.db.models import MemoryItem
from packages.memory.schemas import MemoryFilters, MemoryItemCreate


class MemoryStore:
    """Stores and retrieves memory items across L0-L3 scopes.

    Does NOT call any LLM. Embedding for search uses FakeEmbedding.
    """

    def __init__(self, db: Session) -> None:
        self.db = db

    def put(self, item: MemoryItemCreate) -> MemoryItem:
        memory = MemoryItem(
            memory_id=new_id("mem_"),
            scope=item.scope,
            scope_key=item.scope_key,
            memory_type=item.memory_type,
            content=item.content,
            content_json=item.content_json,  # preserve None vs {}
            embedding=item.embedding,
            importance=item.importance,
            expires_at=item.expires_at,
            source_ref=item.source_ref,  # preserve None vs ""
        )
        self.db.add(memory)
        return memory

    def get_by_scope(self, scope: str, scope_key: str, limit: int = 10) -> list[MemoryItem]:
        stmt = (
            sa_select(MemoryItem)
            .where(MemoryItem.scope == scope, MemoryItem.scope_key == scope_key)
            .order_by(MemoryItem.importance.desc(), MemoryItem.created_at.desc())
            .limit(limit)
        )
        return list(self.db.scalars(stmt).all())

    def search(self, query: str, filters: MemoryFilters, top_k: int = 5) -> list[MemoryItem]:
        clauses: list[ColumnElement[bool]] = []
        if filters.scope:
            clauses.append(MemoryItem.scope == filters.scope)
        if filters.scope_key:
            clauses.append(MemoryItem.scope_key == filters.scope_key)
        if filters.memory_type:
            clauses.append(MemoryItem.memory_type == filters.memory_type)
        if filters.min_importance is not None:
            clauses.append(MemoryItem.importance >= filters.min_importance)

        if filters.service:
            clauses.append(MemoryItem.content_json["service"].as_string() == filters.service)

        # Exclude expired memories
        clauses.append(MemoryItem.expires_at.is_(None) | (MemoryItem.expires_at > utc_now()))

        where = and_(*clauses) if clauses else true()

        try:
            embedding = self._embed_query(query)
            stmt = (
                sa_select(MemoryItem)
                .where(where)
                .where(MemoryItem.embedding.is_not(None))
                .order_by(MemoryItem.embedding.cosine_distance(embedding))
                .limit(top_k)
            )
            return list(self.db.scalars(stmt).all())
        except Exception:
            # pgvector may not be available (e.g. SQLite); fall back to
            # a filtered lexical search that still respects *query*.
            pass

        # Fallback: filter by content LIKE query terms, order by importance
        q_terms = [t for t in query.lower().split() if len(t) > 1]
        if q_terms:
            term_clauses = [MemoryItem.content.ilike(f"%{t}%") for t in q_terms[:5]]
            clauses.append(or_(*term_clauses))

        fallback_where = and_(*clauses) if clauses else true()
        stmt = (
            sa_select(MemoryItem)
            .where(fallback_where)
            .order_by(MemoryItem.importance.desc(), MemoryItem.created_at.desc())
            .limit(top_k)
        )
        return list(self.db.scalars(stmt).all())

    def mark_used(self, memory_id: str, agent_run_id: str) -> None:
        stmt = sa_select(MemoryItem).where(MemoryItem.memory_id == memory_id)
        item = self.db.scalar(stmt)
        if item is not None:
            item.updated_at = utc_now()

    @staticmethod
    def _embed_query(query: str) -> list[float]:
        from packages.rag.embedding_factory import build_embedding_provider
        from packages.common.settings import get_settings

        provider = build_embedding_provider(get_settings())
        embedding = provider.embed_text(query)
        if len(embedding) != provider.dimension:
            import logging
            logging.getLogger(__name__).warning(
                "Memory embedding dimension %d != expected %d; vector search may degrade",
                len(embedding), provider.dimension,
            )
        return embedding
