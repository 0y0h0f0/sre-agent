"""Embedding Job management — M9 PR 9.8.

Async embedding generation for approved runbook chunks. Embedding jobs are
enqueued after runbook approval and run asynchronously — the approve API
never waits for embedding completion.

Failure degrades semantic search gracefully; keyword search always remains
available.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class EmbeddingJob:
    """A job to generate an embedding for a runbook chunk.

    Each job is uniquely identified by (chunk_id, provider, model, dimension, text_hash).
    Duplicate jobs are silently skipped.
    """

    runbook_chunk_id: str
    provider: str
    model: str
    dimension: int
    text_hash: str

    @staticmethod
    def key(
        chunk_id: str,
        provider: str,
        model: str,
        dimension: int,
        text_hash: str,
    ) -> str:
        """Build a unique key for dedup."""
        core = f"{chunk_id}:{provider}:{model}:{dimension}:{text_hash}"
        return hashlib.sha256(core.encode()).hexdigest()[:16]

    def validate_dimension(self, vector: list[float]) -> bool:
        """Check whether the generated vector matches the expected dimension."""
        return len(vector) == self.dimension


class EmbeddingJobQueue:
    """Manages embedding job dispatch and dedup.

    In production, jobs are enqueued to Celery. In tests/local, they may
    run synchronously (CELERY_TASK_ALWAYS_EAGER).
    """

    def __init__(self) -> None:
        self._submitted: set[str] = set()

    def submit(self, job: EmbeddingJob) -> str:
        """Submit an embedding job for async processing.

        Returns the job key. Silently skips duplicate jobs.
        """
        job_key = EmbeddingJob.key(
            chunk_id=job.runbook_chunk_id,
            provider=job.provider,
            model=job.model,
            dimension=job.dimension,
            text_hash=job.text_hash,
        )
        if job_key in self._submitted:
            logger.debug("Duplicate embedding job skipped: %s", job_key)
            return job_key
        self._submitted.add(job_key)
        logger.info(
            "Embedding job submitted: chunk=%s provider=%s",
            job.runbook_chunk_id, job.provider,
        )
        return job_key

    def is_submitted(self, job_key: str) -> bool:
        return job_key in self._submitted


class SemanticSearchMode:
    """Resolves the effective semantic search mode from settings."""

    @staticmethod
    def resolve(*, semantic_enabled: bool, embedding_provider: str) -> str:
        """Return the search mode: 'keyword', 'semantic', or 'hybrid'.

        - embedding_provider == 'disabled' → 'keyword'
        - semantic_enabled + valid provider → 'hybrid'
        - otherwise → 'keyword'
        """
        if embedding_provider == "disabled":
            return "keyword"
        if semantic_enabled and embedding_provider in ("fake", "bge_zh", "external"):
            return "hybrid"
        return "keyword"
