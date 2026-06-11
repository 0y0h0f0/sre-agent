"""Pluggable reranker backends (roadmap Phase 4.2).

Follows the same pattern as packages/tools/trace_backends.py:
a Protocol, a fake backend (current heuristic), and HTTP backends
(Cohere, Jina, BGE) selected via build_reranker_backend(settings).
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any, Protocol

import httpx

from packages.common.errors import ValidationAppError
from packages.common.settings import Settings
from packages.common.time import utc_now

WORD_RE = re.compile(r"[a-z0-9_]+")


class RerankerBackend(Protocol):
    """Protocol for runbook result reranking."""

    name: str

    def rerank(
        self,
        query: str,
        documents: list[dict[str, Any]],
        top_k: int,
    ) -> list[tuple[int, float]]:
        """Re-rank documents and return (original_index, score) pairs."""


class FakeRerankerBackend:
    """Heuristic reranker: vector + metadata + freshness (MVP default)."""

    name = "fake"

    def rerank(
        self,
        query: str,
        documents: list[dict[str, Any]],
        top_k: int,
    ) -> list[tuple[int, float]]:
        scored: list[tuple[int, float]] = []
        for index, doc in enumerate(documents):
            meta = doc.get("metadata", {})
            title = str(doc.get("title", ""))
            vector_score = float(doc.get("score", doc.get("vector_score", 0.5)))
            service = str(doc.get("service", "") or "")
            incident_type = str(doc.get("incident_type", "") or "")
            score = _heuristic_score(
                query=query,
                metadata=meta,
                title=title,
                vector_score=vector_score,
                service=service or None,
                incident_type=incident_type or None,
            )
            scored.append((index, score))
        scored.sort(key=lambda item: (-item[1], item[0]))
        return scored[:top_k]


class CohereRerankerBackend:
    """Cohere Rerank API (rerank-english-v3.0 or multilingual)."""

    name = "cohere"

    def __init__(
        self,
        *,
        api_key: str,
        model: str = "rerank-english-v3.0",
        timeout: float = 5.0,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    def rerank(
        self,
        query: str,
        documents: list[dict[str, Any]],
        top_k: int,
    ) -> list[tuple[int, float]]:
        texts = [doc.get("text", doc.get("content", "")) for doc in documents]
        try:
            response = httpx.post(
                "https://api.cohere.com/v2/rerank",
                json={
                    "model": self.model,
                    "query": query,
                    "documents": texts,
                    "top_n": top_k,
                },
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=self.timeout,
            )
            response.raise_for_status()
            payload = response.json()
            results: list[tuple[int, float]] = []
            for item in payload.get("results", []):
                results.append((item["index"], item["relevance_score"]))
            return results
        except Exception as exc:
            raise RuntimeError(f"Cohere rerank failed: {exc}") from exc


class JinaRerankerBackend:
    """Jina Reranker API (jina-reranker-v2-base-multilingual)."""

    name = "jina"

    def __init__(
        self,
        *,
        base_url: str,
        model: str = "jina-reranker-v2-base-multilingual",
        timeout: float = 5.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout

    def rerank(
        self,
        query: str,
        documents: list[dict[str, Any]],
        top_k: int,
    ) -> list[tuple[int, float]]:
        texts = [doc.get("text", doc.get("content", "")) for doc in documents]
        try:
            response = httpx.post(
                f"{self.base_url}/rerank",
                json={
                    "model": self.model,
                    "query": query,
                    "documents": texts,
                    "top_n": top_k,
                },
                timeout=self.timeout,
            )
            response.raise_for_status()
            payload = response.json()
            results: list[tuple[int, float]] = []
            for item in payload.get("results", []):
                results.append((item["index"], item["relevance_score"]))
            return results
        except Exception as exc:
            raise RuntimeError(f"Jina rerank failed: {exc}") from exc


class BGERerankerBackend:
    """BGE Reranker (BAAI/bge-reranker-v2-m3) via TEI-compatible API."""

    name = "bge"

    def __init__(
        self,
        *,
        base_url: str,
        model: str = "BAAI/bge-reranker-v2-m3",
        timeout: float = 5.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout

    def rerank(
        self,
        query: str,
        documents: list[dict[str, Any]],
        top_k: int,
    ) -> list[tuple[int, float]]:
        texts = [doc.get("text", doc.get("content", "")) for doc in documents]
        try:
            response = httpx.post(
                f"{self.base_url}/rerank",
                json={
                    "query": query,
                    "texts": texts,
                },
                timeout=self.timeout,
            )
            response.raise_for_status()
            # TEI returns list of scores in document order
            scores = response.json()
            if isinstance(scores, list):
                scored = [(index, float(score)) for index, score in enumerate(scores)]
                scored.sort(key=lambda item: (-item[1], item[0]))
                return scored[:top_k]
            return []
        except Exception as exc:
            raise RuntimeError(f"BGE rerank failed: {exc}") from exc


def build_reranker_backend(settings: Settings) -> RerankerBackend:
    """Construct a reranker backend from settings.

    Provider selection (reranker_provider):
      - fake   → FakeRerankerBackend (heuristic, default)
      - cohere → CohereRerankerBackend
      - jina   → JinaRerankerBackend
      - bge    → BGERerankerBackend
    """
    provider = settings.reranker_provider.strip().lower()

    if provider == "fake":
        return FakeRerankerBackend()

    if provider == "cohere":
        api_key = (
            settings.reranker_cohere_api_key.get_secret_value()
            if settings.reranker_cohere_api_key else None
        )
        if not api_key:
            raise ValidationAppError(
                "reranker_cohere_api_key is required when reranker_provider=cohere"
            )
        return CohereRerankerBackend(
            api_key=api_key,
            model=settings.reranker_cohere_model,
            timeout=settings.tool_timeout_seconds,
        )

    if provider == "jina":
        return JinaRerankerBackend(
            base_url=settings.reranker_jina_base_url,
            model=settings.reranker_jina_model,
            timeout=settings.tool_timeout_seconds,
        )

    if provider == "bge":
        return BGERerankerBackend(
            base_url=settings.reranker_bge_base_url,
            model=settings.reranker_bge_model,
            timeout=settings.tool_timeout_seconds,
        )

    supported = {"fake", "cohere", "jina", "bge"}
    raise ValidationAppError(
        f"unknown reranker_provider '{settings.reranker_provider}'",
        details={"supported": sorted(supported)},
    )


# ---------------------------------------------------------------------------
# Heuristic scoring helpers (moved from reranker.py)
# ---------------------------------------------------------------------------


def _heuristic_score(
    *,
    query: str,
    metadata: dict[str, Any],
    title: str,
    vector_score: float,
    service: str | None,
    incident_type: str | None,
) -> float:
    service_match = (
        1.0
        if service and (metadata.get("service") or "").lower() == service.lower()
        else 0.0
    )
    incident_type_match = (
        1.0 if incident_type and metadata.get("incident_type") == incident_type else 0.0
    )
    title_keyword_match = _title_keyword_match(query, title)
    freshness_score = _freshness_score(metadata.get("updated_at"))
    score = (
        _clamp01(vector_score) * 0.65
        + service_match * 0.15
        + incident_type_match * 0.10
        + title_keyword_match * 0.05
        + freshness_score * 0.05
    )
    return round(score, 6)


def _title_keyword_match(query: str, title: str) -> float:
    query_terms = set(WORD_RE.findall(query.lower()))
    if not query_terms:
        return 0.0
    title_terms = set(WORD_RE.findall(title.lower()))
    if not title_terms:
        return 0.0
    return len(query_terms & title_terms) / len(query_terms)


def _freshness_score(value: object) -> float:
    if not value:
        return 0.0
    try:
        parsed = date.fromisoformat(str(value))
    except ValueError:
        return 0.0
    age_days = max(0, (utc_now().date() - parsed).days)
    if age_days <= 30:
        return 1.0
    if age_days <= 180:
        return 0.8
    if age_days <= 365:
        return 0.6
    return 0.4


def _clamp01(value: float) -> float:
    return min(1.0, max(0.0, value))
