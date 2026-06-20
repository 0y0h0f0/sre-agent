"""Runbook search tool wrapper.

The RAG retriever owns ranking and storage details. This wrapper adapts results
to the common ToolResult/evidence shape so the agent can cite runbook chunks the
same way it cites metrics, logs, traces, and other evidence.
"""

from __future__ import annotations

import hashlib
import json

from pydantic import BaseModel

from packages.rag.retriever import RunbookRetriever
from packages.rag.retriever import RunbookSearchQuery as RunbookSearchQuery
from packages.tools.base import ToolResult, compact_summary, elapsed_ms, start_timer
from packages.tools.cache import RequestLocalToolCache


class RunbookSearchTool:
    """BaseTool-compatible wrapper around ``RunbookRetriever``."""

    name = "runbook_search"

    def __init__(
        self,
        *,
        retriever: RunbookRetriever,
        timeout_seconds: float = 2.0,
        cache: RequestLocalToolCache | None = None,
    ) -> None:
        self.retriever = retriever
        self.timeout_seconds = timeout_seconds
        self.cache = cache

    def run(self, query: BaseModel) -> ToolResult:
        search_query = RunbookSearchQuery.model_validate(query)
        started_at = start_timer()
        cache_key = _cache_key(search_query)
        cached = self.cache.get(cache_key) if self.cache else None
        if cached is not None:
            return cached.model_copy(update={"duration_ms": elapsed_ms(started_at)})

        try:
            results = self.retriever.search(search_query)
            data = {"results": [result.model_dump() for result in results], "count": len(results)}
            result = ToolResult(
                status="succeeded",
                data=data,
                summary=compact_summary(
                    {
                        "query": search_query.query,
                        "service": search_query.service,
                        "incident_type": search_query.incident_type,
                        "matches": len(results),
                    }
                ),
                evidence=[
                    # chunk_id/source_path/metadata are required for diagnosis
                    # auditability: root cause and reports can point back to
                    # the exact runbook text, not just a natural-language quote.
                    {
                        "type": "runbook",
                        "source": "runbook",
                        "source_id": item.chunk_id,
                        "title": item.title,
                        "excerpt": item.excerpt,
                        "payload": {
                            "chunk_id": item.chunk_id,
                            "source_path": item.source_path,
                            "metadata": item.metadata,
                            "score": item.score,
                        },
                        "confidence": item.score,
                    }
                    for item in results
                ],
                cache_key=cache_key,
                duration_ms=elapsed_ms(started_at),
            )
        except Exception as exc:
            # RAG degradation should not block the incident workflow. The agent
            # can still diagnose from live evidence and record that runbook
            # search was unavailable.
            result = ToolResult(
                status="degraded",
                data={"query": search_query.model_dump(mode="json")},
                summary=f"Runbook search unavailable for {search_query.query}",
                evidence=[],
                cache_key=cache_key,
                duration_ms=elapsed_ms(started_at),
                error_message=str(exc),
            )

        if self.cache and result.status == "succeeded":
            self.cache.set(cache_key, result)
        return result


def _cache_key(query: RunbookSearchQuery) -> str:
    """Build a stable non-time-bucketed key for semantic runbook search."""
    payload = query.model_dump(mode="json", exclude_none=True)
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:16]
    return f"tool:runbook_search:{digest}"
