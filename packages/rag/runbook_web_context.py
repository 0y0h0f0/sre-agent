"""Runbook Web Context Builder — M9 PR 9.4.

Orchestrates safe web search for runbook draft enrichment. Queries are redacted
before sending, URLs are validated for safety, and results are traceable with
source URLs, final URLs, content hashes, and retrieval timestamps.

Results are evidence for draft review only — never auto-published.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

from packages.common.backend_url_safety import BackendUrlSafetyValidator
from packages.common.feature_flags import is_m9_subfeature_enabled
from packages.common.redaction import redact_text
from packages.common.settings import Settings
from packages.rag.web_search_provider import (
    build_web_search_provider,
)

logger = logging.getLogger(__name__)


@dataclass
class WebSearchResult:
    """A single search result with full traceability metadata."""

    title: str
    original_url: str
    final_url: str
    snippet: str
    content_hash: str
    provider: str
    redaction_version: str
    retrieved_at: str = ""


@dataclass
class WebContextResult:
    """Result of building web context for runbook enrichment."""

    status: str  # "ok" | "disabled" | "degraded" | "blocked"
    purpose: str = "draft_enrichment"
    results: list[WebSearchResult] = field(default_factory=list)
    query_redacted: str = ""
    error_message: str | None = None


class RunbookWebContextBuilder:
    """Build runbook web context with full safety controls.

    1. Check feature gates (M9 + RUNBOOK_WEB_SEARCH)
    2. Redact query text
    3. Validate provider is not disabled
    4. Execute search via provider
    5. Validate result URLs for safety
    6. Return traceable results (evidence-only, no auto-publish)
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or Settings()

    def build_context(
        self,
        *,
        query: str,
        purpose: str = "draft_enrichment",
    ) -> WebContextResult:
        """Execute a safe web search and return context for draft enrichment.

        Args:
            query: The search query (will be redacted before sending).
            purpose: Intended use of results ("draft_enrichment" only).

        Returns:
            WebContextResult with traceable search results.
        """
        # 1. Feature gate check.
        if not is_m9_subfeature_enabled(self.settings, "runbook_web_search"):
            return WebContextResult(status="disabled", purpose=purpose)

        # 2. Provider must not be disabled.
        provider_name = self.settings.runbook_web_search_provider.strip().lower()
        if provider_name == "disabled":
            return WebContextResult(
                status="degraded",
                purpose=purpose,
                error_message="web_search provider is disabled",
            )

        # 3. Production requires allowed domains.
        if self.settings.app_env == "production":
            allowed = self.settings.runbook_web_search_allowed_domains.strip()
            if not allowed:
                return WebContextResult(
                    status="blocked",
                    purpose=purpose,
                    error_message="web_search requires allowed domains in production",
                )

        # 4. Redact query.
        redacted_query = redact_text(query).redacted_text

        # 5. Execute search.
        provider = build_web_search_provider(self.settings)
        try:
            response = provider.search(redacted_query)
        except Exception:
            logger.warning("Web search provider failed", exc_info=True)
            return WebContextResult(
                status="degraded",
                purpose=purpose,
                error_message="web_search provider exception",
            )

        if response.status != "ok":
            return WebContextResult(
                status="degraded",
                purpose=purpose,
                error_message=response.error_message or "web_search returned no results",
            )

        # 6. Validate result URLs for safety.
        url_validator = BackendUrlSafetyValidator(app_env=self.settings.app_env)
        safe_results: list[WebSearchResult] = []
        for item in response.results:
            validation = url_validator.validate(item.final_url)
            if not validation.is_safe:
                logger.warning("Blocked unsafe URL in web search result: %s", item.final_url)
                continue
            safe_results.append(WebSearchResult(
                title=item.title,
                original_url=item.original_url,
                final_url=item.final_url,
                snippet=item.snippet[:500],
                content_hash=item.content_hash,
                provider=item.provider,
                redaction_version=item.redaction_version,
                retrieved_at=item.retrieved_at or datetime.now(UTC).isoformat(),
            ))

        return WebContextResult(
            status="ok",
            purpose=purpose,
            results=safe_results,
            query_redacted=redacted_query[:200],
        )
