"""Runbook retrieval over stored chunks."""

from __future__ import annotations

import hashlib
import json
import re
from math import sqrt
from typing import Any

from pydantic import BaseModel, Field, field_validator

from packages.db.models import RunbookChunk
from packages.db.repositories.runbooks import RunbookChunkRepository
from packages.rag.bm25 import adaptive_alpha, build_tsquery, normalize_bm25
from packages.rag.embedding_factory import EmbeddingProvider, FakeEmbeddingProvider
from packages.rag.reranker import rerank_score

WORD_RE = re.compile(r"[a-z0-9_]+")


class RunbookSearchQuery(BaseModel):
    query: str = Field(min_length=1)
    service: str | None = None
    incident_type: str | None = None
    top_k: int = Field(default=5, ge=1, le=20)

    @field_validator("query", "service", "incident_type", mode="before")
    @classmethod
    def _strip_text(cls, value: object) -> object:
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                return None
            return stripped
        return value

    @field_validator("incident_type")
    @classmethod
    def _normalize_incident_type(cls, value: str | None) -> str | None:
        return value.lower() if value else None


class RunbookSearchResult(BaseModel):
    chunk_id: str
    source_path: str
    title: str
    excerpt: str
    score: float
    metadata: dict[str, Any] = Field(default_factory=dict)


RunbookSearchResultList = list[RunbookSearchResult]


class RunbookSearchCache:
    def __init__(self) -> None:
        self._items: dict[str, RunbookSearchResultList] = {}

    def get(self, key: str) -> RunbookSearchResultList | None:
        cached = self._items.get(key)
        if cached is None:
            return None
        return [item.model_copy(deep=True) for item in cached]

    def set(self, key: str, results: RunbookSearchResultList) -> None:
        self._items[key] = [item.model_copy(deep=True) for item in results]


class RunbookRetriever:
    def __init__(
        self,
        repository: RunbookChunkRepository,
        *,
        embedding_provider: EmbeddingProvider | None = None,
        cache: RunbookSearchCache | None = None,
        use_hybrid: bool = True,
    ) -> None:
        self.repository = repository
        self.embedding_provider: EmbeddingProvider = embedding_provider or FakeEmbeddingProvider()
        self.cache = cache
        self.use_hybrid = use_hybrid

    def search(self, query: RunbookSearchQuery) -> RunbookSearchResultList:
        normalized_query = RunbookSearchQuery.model_validate(query)
        cache_key = _search_cache_key(normalized_query)
        if self.cache is not None:
            cached = self.cache.get(cache_key)
            if cached is not None:
                return cached

        query_embedding = self.embedding_provider.embed_text(normalized_query.query)
        all_chunks: dict[str, tuple[RunbookChunk, float]] = {}

        # --- Vector recall (always runs) ---
        for chunk in self.repository.list_chunks():
            if not _matches_metadata(chunk.metadata_json, normalized_query):
                continue
            vec_score = max(
                _normalized_cosine(query_embedding, chunk.embedding),
                _lexical_score(normalized_query.query, f"{chunk.title}\n{chunk.content}"),
            )
            all_chunks[chunk.chunk_id] = (chunk, vec_score)

        # --- BM25 recall (optional hybrid path) ---
        bm25_scores: dict[str, float] = {}
        if self.use_hybrid:
            tsquery = build_tsquery(normalized_query.query)
            for chunk, bm25_raw in self.repository.search_bm25(
                tsquery,
                service=normalized_query.service,
                incident_type=normalized_query.incident_type,
            ):
                bm25_scores[chunk.chunk_id] = normalize_bm25(bm25_raw)
                # Include BM25-only chunks that may not be in vector results
                if chunk.chunk_id not in all_chunks:
                    all_chunks[chunk.chunk_id] = (chunk, 0.0)

        # --- Hybrid score fusion ---
        candidates: list[tuple[RunbookChunk, float]] = []
        if bm25_scores:
            titles_for_alpha = [chunk.title for chunk, _ in all_chunks.values()]
            alpha = adaptive_alpha(normalized_query.query, titles_for_alpha)
            for chunk_id, (chunk, vec_score) in all_chunks.items():
                bm25 = bm25_scores.get(chunk_id, 0.0)
                hybrid = alpha * bm25 + (1.0 - alpha) * vec_score
                candidates.append((chunk, hybrid))
        else:
            candidates = list(all_chunks.values())

        recalled = sorted(candidates, key=lambda item: item[1], reverse=True)[:20]
        ranked = sorted(
            (
                (
                    chunk,
                    rerank_score(
                        query=normalized_query.query,
                        metadata=chunk.metadata_json,
                        title=chunk.title,
                        vector_score=vector_score,
                        service=normalized_query.service,
                        incident_type=normalized_query.incident_type,
                    ),
                )
                for chunk, vector_score in recalled
            ),
            key=lambda item: (-item[1], item[0].chunk_id),
        )
        results = [
            RunbookSearchResult(
                chunk_id=chunk.chunk_id,
                source_path=chunk.source_path,
                title=chunk.title,
                excerpt=_excerpt(chunk.content, normalized_query.query),
                score=score,
                metadata=dict(chunk.metadata_json),
            )
            for chunk, score in ranked[: normalized_query.top_k]
        ]
        if self.cache is not None:
            self.cache.set(cache_key, results)
        return results


def format_runbook_context(results: list[RunbookSearchResult]) -> str:
    blocks = []
    for result in results:
        title = result.title.replace('"', "'")
        blocks.append(
            f'[chunk_id={result.chunk_id} source={result.source_path} title="{title}"]\n'
            f"{result.excerpt}"
        )
    return "\n\n".join(blocks)


def _matches_metadata(metadata: dict[str, Any], query: RunbookSearchQuery) -> bool:
    if query.service and (metadata.get("service") or "").lower() != query.service.lower():
        return False
    if query.incident_type and metadata.get("incident_type") != query.incident_type:
        return False
    return True


def _normalized_cosine(left: list[float], right: list[float]) -> float:
    if not left or not right:
        return 0.0
    count = min(len(left), len(right))
    dot = sum(float(left[index]) * float(right[index]) for index in range(count))
    left_norm = sqrt(sum(float(value) * float(value) for value in left[:count]))
    right_norm = sqrt(sum(float(value) * float(value) for value in right[:count]))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return (dot / (left_norm * right_norm) + 1.0) / 2.0


def _lexical_score(query: str, text: str) -> float:
    query_terms = set(WORD_RE.findall(query.lower()))
    if not query_terms:
        return 0.0
    text_terms = set(WORD_RE.findall(text.lower()))
    if not text_terms:
        return 0.0
    overlap = len(query_terms & text_terms) / len(query_terms)
    return min(1.0, overlap)


def _excerpt(content: str, query: str, *, limit: int = 360) -> str:
    compact = " ".join(content.split())
    if len(compact) <= limit:
        return compact
    terms = WORD_RE.findall(query.lower())
    lower = compact.lower()
    first_index = min((lower.find(term) for term in terms if lower.find(term) >= 0), default=0)
    start = max(0, first_index - 80)
    end = min(len(compact), start + limit)
    prefix = "..." if start > 0 else ""
    suffix = "..." if end < len(compact) else ""
    return f"{prefix}{compact[start:end].strip()}{suffix}"


def _search_cache_key(query: RunbookSearchQuery) -> str:
    payload = query.model_dump(mode="json", exclude_none=True)
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:24]
    return (
        f"runbook_search:{digest}:{query.service or '*'}:{query.incident_type or '*'}:{query.top_k}"
    )
