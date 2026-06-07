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
from packages.common.settings import Settings


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Protocol for embedding providers used by RunbookRetriever and RunbookIngestor."""

    dimension: int
    model_name: str

    def embed_text(self, text: str) -> list[float]: ...

    def embed_many(self, texts: list[str]) -> list[list[float]]: ...


class FakeEmbeddingProvider:
    """Deterministic embedding provider for tests and local dev.

    Generates stable 384-dimension normalized vectors from text via repeated
    SHA256 hashing.  No external service required.
    """

    dimension = 384
    model_name = "fake-384"

    def embed_text(self, text: str) -> list[float]:
        return _fake_embed(text, self.dimension)

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
            )
            response.raise_for_status()
            return response.json()  # type: ignore[no-any-return]
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
            )
            response.raise_for_status()
            return response.json()  # type: ignore[no-any-return]
        except Exception as exc:
            raise RuntimeError(
                f"Text2Vec embedding failed for {len(texts)} texts: {exc}"
            ) from exc


def build_embedding_provider(settings: Settings) -> EmbeddingProvider:
    """Construct an embedding provider from settings.

    Provider selection (embedding_provider):
      - fake       → FakeEmbeddingProvider (deterministic, default)
      - bge_zh     → BGEZhEmbeddingProvider (BAAI/bge-small-zh, 512-dim)
      - text2vec   → Text2VecEmbeddingProvider (text2vec-large-chinese, 1024-dim)
    """
    provider = settings.embedding_provider.strip().lower()

    if provider == "fake":
        return FakeEmbeddingProvider()

    if provider == "bge_zh":
        return BGEZhEmbeddingProvider(
            base_url=settings.embedding_bge_zh_url,
            timeout=settings.tool_timeout_seconds,
        )

    if provider == "text2vec":
        return Text2VecEmbeddingProvider(
            base_url=settings.embedding_text2vec_url,
            timeout=settings.tool_timeout_seconds,
        )

    supported = {"fake", "bge_zh", "text2vec"}
    raise ValidationAppError(
        f"unknown embedding_provider '{settings.embedding_provider}'",
        details={"supported": sorted(supported)},
    )


# ---------------------------------------------------------------------------
# Internal helpers (matching FakeEmbedding algorithm for backward compat)
# ---------------------------------------------------------------------------


def _fake_embed(text: str, dimension: int) -> list[float]:
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
