"""Web search providers — M9 PR 9.4.

Provider abstraction for safe web search. Each provider must validate URLs,
redact queries, respect timeouts, and return traceable results.

Providers:
- disabled: always returns degraded (no fallback to default)
- fake: returns deterministic fake results for CI/local testing
- exa: Exa API provider (future, requires API key)
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

from packages.common.redaction import redact_text
from packages.common.settings import Settings

SUPPORTED_WEB_SEARCH_PROVIDERS = frozenset({"disabled", "fake"})

# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class WebSearchResultItem:
    title: str
    original_url: str
    final_url: str
    snippet: str
    content_hash: str
    provider: str
    redaction_version: str
    retrieved_at: str = ""
    redirect_chain: list[str] = field(default_factory=list)


@dataclass
class WebSearchResponse:
    status: str  # "ok" | "degraded"
    results: list[WebSearchResultItem] = field(default_factory=list)
    query_redacted: str = ""
    error_message: str | None = None


# ---------------------------------------------------------------------------
# Provider protocol
# ---------------------------------------------------------------------------


class WebSearchProvider(Protocol):
    """Protocol for web search providers."""

    name: str

    def search(self, query: str) -> WebSearchResponse:
        """Execute a web search with safety controls."""


# ---------------------------------------------------------------------------
# Disabled provider
# ---------------------------------------------------------------------------


class DisabledWebSearchProvider:
    """Always returns degraded — no fallback to default."""

    name = "disabled"

    def search(self, query: str) -> WebSearchResponse:
        return WebSearchResponse(
            status="degraded",
            error_message="web_search provider is disabled",
        )


# ---------------------------------------------------------------------------
# Fake provider (CI / local testing)
# ---------------------------------------------------------------------------


class FakeWebSearchProvider:
    """Returns deterministic fake results for testing — never calls external service."""

    name = "fake"
    _REDACTION_VERSION = "m9-9.4-1"

    def __init__(self, *, max_results: int = 5) -> None:
        self._max_results = max(0, max_results)

    def search(self, query: str) -> WebSearchResponse:
        redacted = redact_text(query)
        now = datetime.now(UTC).isoformat()

        results: list[WebSearchResultItem] = []
        urls = self._fake_urls(query)[: self._max_results]

        for i, url in enumerate(urls):
            content = f"Fake result {i+1} for query: {redacted.redacted_text[:50]}"
            results.append(WebSearchResultItem(
                title=f"Fake Result {i+1}: {redacted.redacted_text[:40]}",
                original_url=url,
                final_url=url,
                snippet=content[:200],
                content_hash=hashlib.sha256(content.encode()).hexdigest()[:16],
                provider="fake",
                redaction_version=self._REDACTION_VERSION,
                retrieved_at=now,
            ))

        return WebSearchResponse(
            status="ok" if results else "degraded",
            results=results,
            query_redacted=redacted.redacted_text[:200],
        )

    @staticmethod
    def _fake_urls(query: str) -> list[str]:
        """Generate safe fake URLs for testing."""
        return [
            "https://docs.example.com/sre/runbook-1",
            "https://sre-handbook.example.com/high-5xx",
            "https://kb.internal.example.org/incident-response",
        ]


# ---------------------------------------------------------------------------
# Provider factory
# ---------------------------------------------------------------------------


def build_web_search_provider(settings: Settings) -> WebSearchProvider:
    """Build a web search provider from settings."""
    provider = settings.runbook_web_search_provider.strip().lower()

    if provider == "disabled":
        return DisabledWebSearchProvider()

    if provider == "fake":
        return FakeWebSearchProvider(max_results=settings.runbook_web_search_max_results)

    # Unknown and future providers must not silently fall back to a real default.
    return DisabledWebSearchProvider()
