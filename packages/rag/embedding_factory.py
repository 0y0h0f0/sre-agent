"""Embedding provider factory.

Follows the same pattern as packages/agent/llm/factory.py (build_llm):
settings-driven dispatch returning a Protocol-conforming provider.
"""

from __future__ import annotations

import hashlib
from math import sqrt
from typing import Protocol, runtime_checkable

import httpx

from packages.common.errors import ValidationAppError
from packages.common.feature_flags import is_m9_subfeature_enabled
from packages.common.settings import Settings


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Protocol for embedding providers used by RunbookRetriever and RunbookIngestor.

    Providers must expose their vector dimension because the primary
    ``runbook_chunks.embedding`` column is currently ``vector(512)``.
    """

    dimension: int
    model_name: str

    def embed_text(self, text: str) -> list[float]: ...

    def embed_many(self, texts: list[str]) -> list[list[float]]: ...


class FakeEmbeddingProvider:
    """Deterministic embedding provider for tests and local dev.

    Generates stable 512-dimension normalized vectors from text via repeated
    SHA256 hashing.  No external service required.
    """

    dimension = 512
    model_name = "fake-512"

    def embed_text(self, text: str) -> list[float]:
        return _fake_embed(text, self.dimension)

    def embed_many(self, texts: list[str]) -> list[list[float]]:
        return [self.embed_text(text) for text in texts]


class DisabledEmbeddingProvider:
    """Keyword-only fallback provider.

    The primary runbook chunk table requires a 512-dim vector column, so disabled
    embedding still returns a deterministic placeholder vector. Retrieval then
    relies on lexical/BM25 scoring instead of semantic distance.
    """

    dimension = 512
    model_name = "disabled"

    def embed_text(self, text: str) -> list[float]:
        return [0.0] * self.dimension

    def embed_many(self, texts: list[str]) -> list[list[float]]:
        return [self.embed_text(text) for text in texts]


class BGEZhEmbeddingProvider:
    """BAAI/bge-small-zh via local TEI or compatible HTTP API."""

    dimension = 512
    model_name = "bge-small-zh"

    def __init__(self, *, base_url: str, timeout: float = 5.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def embed_text(self, text: str) -> list[float]:
        result = self.embed_many([text])
        return result[0]

    def embed_many(self, texts: list[str]) -> list[list[float]]:
        try:
            response = httpx.post(
                f"{self.base_url}/embed",
                json={"inputs": texts},
                timeout=self.timeout,
                trust_env=False,
            )
            response.raise_for_status()
            vectors = response.json()
            # Validate dimensions before returning so callers never attempt to
            # write incompatible vectors into the primary pgvector column.
            return _require_dimension(vectors, self.dimension, self.model_name)
        except Exception as exc:
            raise RuntimeError(
                f"BGE-ZH embedding failed for {len(texts)} texts: {exc}"
            ) from exc


class Text2VecEmbeddingProvider:
    """text2vec-large-chinese via local HTTP API."""

    dimension = 1024
    model_name = "text2vec-large-chinese"

    def __init__(self, *, base_url: str, timeout: float = 5.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def embed_text(self, text: str) -> list[float]:
        result = self.embed_many([text])
        return result[0]

    def embed_many(self, texts: list[str]) -> list[list[float]]:
        try:
            response = httpx.post(
                f"{self.base_url}/embed",
                json={"sentences": texts},
                timeout=self.timeout,
                trust_env=False,
            )
            response.raise_for_status()
            vectors = response.json()
            # This provider is kept for future side-table/migration work; the
            # factory currently rejects it for primary 512-dim writes.
            return _require_dimension(vectors, self.dimension, self.model_name)
        except Exception as exc:
            raise RuntimeError(
                f"Text2Vec embedding failed for {len(texts)} texts: {exc}"
            ) from exc


def build_embedding_provider(settings: Settings) -> EmbeddingProvider:
    """Construct an embedding provider from settings.

    Provider selection (embedding_provider):
      - disabled   → DisabledEmbeddingProvider (keyword-only fallback)
      - fake       → FakeEmbeddingProvider (deterministic, default)
      - bge_zh     → BGEZhEmbeddingProvider (BAAI/bge-small-zh, 512-dim)
      - text2vec   → currently rejected for the primary 512-dim chunk store
      - external   → M9 external provider only when its gates and URL are explicit
    """
    provider = settings.embedding_provider.strip().lower()

    if provider == "disabled":
        return DisabledEmbeddingProvider()

    if provider == "fake":
        return FakeEmbeddingProvider()

    if provider == "bge_zh":
        return BGEZhEmbeddingProvider(
            base_url=settings.embedding_bge_zh_url,
            timeout=settings.tool_timeout_seconds,
        )

    if provider == "text2vec":
        raise ValidationAppError(
            "embedding_provider='text2vec' is 1024-dimensional and cannot "
            "write to the primary runbook_chunks.embedding vector(512) column",
            details={
                "provider": "text2vec",
                "provider_dimension": 1024,
                "required_dimension": 512,
            },
        )

    if provider == "external":
        # External embedding is an M9 capability and is off unless both semantic
        # search and the subfeature gate are explicitly enabled. Disabled fallback
        # preserves lexical/BM25 behavior without making network calls.
        if (
            not settings.semantic_runbook_search_enabled
            or not is_m9_subfeature_enabled(settings, "external_embedding_provider")
        ):
            return DisabledEmbeddingProvider()
        if not settings.external_embedding_url.strip():
            raise ValidationAppError(
                "embedding_provider='external' requires EXTERNAL_EMBEDDING_URL",
                details={"required_setting": "EXTERNAL_EMBEDDING_URL"},
            )
        from packages.rag.external_embedding_provider import ExternalEmbeddingProvider

        return ExternalEmbeddingProvider(
            endpoint=settings.external_embedding_url,
            secret_ref=settings.external_embedding_secret_ref,
            timeout_seconds=settings.embedding_timeout_seconds,
            app_env=settings.app_env,
            allowed_domain_patterns=_parse_csv(settings.external_embedding_allowed_domains),
            blocked_domain_patterns=_parse_csv(settings.external_embedding_blocked_domains),
        )

    supported = {"disabled", "fake", "bge_zh", "text2vec", "external"}
    raise ValidationAppError(
        f"unknown embedding_provider '{settings.embedding_provider}'",
        details={"supported": sorted(supported)},
    )


def _require_dimension(
    vectors: object,
    dimension: int,
    model_name: str,
) -> list[list[float]]:
    """Validate and coerce provider output to a list of float vectors."""
    if not isinstance(vectors, list):
        raise ValueError(f"{model_name} response must be a list of vectors")
    checked: list[list[float]] = []
    for index, vector in enumerate(vectors):
        if not isinstance(vector, list):
            raise ValueError(f"{model_name} vector {index} is not a list")
        if len(vector) != dimension:
            raise ValueError(
                f"{model_name} vector {index} dimension {len(vector)} != {dimension}"
            )
        checked.append([float(value) for value in vector])
    return checked


def _parse_csv(value: str) -> list[str]:
    """Parse comma-separated domain pattern settings."""
    return [item.strip() for item in value.split(",") if item.strip()] if value else []


# ---------------------------------------------------------------------------
# Internal helpers (matching FakeEmbedding algorithm for backward compat)
# ---------------------------------------------------------------------------


def _fake_embed(text: str, dimension: int) -> list[float]:
    """Generate a stable normalized pseudo-embedding from text.

    This is not semantically meaningful like a model embedding, but it is stable
    across processes and suitable for deterministic tests.
    """
    values: list[float] = []
    counter = 0
    normalized = " ".join(text.strip().lower().split())
    while len(values) < dimension:
        digest = hashlib.sha256(f"{normalized}\x1f{counter}".encode()).digest()
        for index in range(0, len(digest), 2):
            if len(values) >= dimension:
                break
            raw = int.from_bytes(digest[index : index + 2], "big", signed=False)
            values.append((raw / 65535.0) * 2.0 - 1.0)
        counter += 1

    norm = sqrt(sum(value * value for value in values)) or 1.0
    return [round(value / norm, 12) for value in values]
