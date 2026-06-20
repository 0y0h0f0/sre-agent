"""Deterministic fake embeddings for local tests and demos.

Deprecated: prefer FakeEmbeddingProvider from packages.rag.embedding_factory.
This class is kept for backward compatibility with existing imports; new code
should use build_embedding_provider(settings) instead.
"""

from __future__ import annotations

import hashlib
from math import sqrt


class FakeEmbedding:
    """Generate deterministic normalized vectors without external services.

    Legacy compatibility shim. Keep the algorithm aligned with
    FakeEmbeddingProvider so old imports and new factory-built providers produce
    identical vectors in tests.
    """

    dimension = 512
    model_name = "fake-512"

    def embed_text(self, text: str) -> list[float]:
        """Return a stable 512-dim normalized pseudo-vector."""
        values: list[float] = []
        counter = 0
        normalized = " ".join(text.strip().lower().split())
        while len(values) < self.dimension:
            digest = hashlib.sha256(f"{normalized}\x1f{counter}".encode()).digest()
            for index in range(0, len(digest), 2):
                if len(values) >= self.dimension:
                    break
                raw = int.from_bytes(digest[index : index + 2], "big", signed=False)
                values.append((raw / 65535.0) * 2.0 - 1.0)
            counter += 1

        norm = sqrt(sum(value * value for value in values)) or 1.0
        return [round(value / norm, 12) for value in values]

    def embed_many(self, texts: list[str]) -> list[list[float]]:
        """Embed multiple texts without batching side effects."""
        return [self.embed_text(text) for text in texts]
