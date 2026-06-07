"""Runbook result reranking — compatibility shim.

Deprecated: prefer FakeRerankerBackend from packages.rag.reranker_backends.
This module is kept for backward compatibility; new code should use
build_reranker_backend(settings) instead.
"""

from __future__ import annotations

from typing import Any

from packages.rag.reranker_backends import FakeRerankerBackend

_fake = FakeRerankerBackend()


def rerank_score(
    *,
    query: str,
    metadata: dict[str, Any],
    title: str,
    vector_score: float,
    service: str | None,
    incident_type: str | None,
) -> float:
    """Re-rank a single document. Delegates to FakeRerankerBackend."""
    results = _fake.rerank(
        query=query,
        documents=[
            {
                "metadata": metadata,
                "title": title,
                "score": vector_score,
                "service": service or "",
                "incident_type": incident_type or "",
            }
        ],
        top_k=1,
    )
    if results:
        return results[0][1]
    return 0.0
