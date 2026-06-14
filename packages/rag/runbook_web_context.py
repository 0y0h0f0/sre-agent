"""Runbook Web Context Builder — M9 PR 9.4.

Orchestrates safe web search for runbook draft enrichment. Queries are redacted
before sending, URLs are validated for safety, and results are traceable with
source URLs, final URLs, content hashes, and retrieval timestamps.

Results are evidence for draft review only — never auto-published.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import urlparse

from packages.common.backend_url_safety import BackendUrlSafetyValidator
from packages.common.feature_flags import is_m9_subfeature_enabled
from packages.common.metrics import AgentMetricsCollector
from packages.common.redaction import redact_text
from packages.common.settings import Settings
from packages.rag.web_search_provider import (
    SUPPORTED_WEB_SEARCH_PROVIDERS,
    WebSearchProvider,
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

    status: str  # "ok" | "disabled" | "config_error" | "degraded" | "blocked"
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

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        provider: WebSearchProvider | None = None,
        dns_resolver: Callable[[str], Iterable[str]] | None = None,
    ) -> None:
        self.settings = settings or Settings()
        self._provider = provider
        self._dns_resolver = dns_resolver

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
        if purpose != "draft_enrichment":
            return self._blocked(
                purpose=purpose,
                reason="unsupported_purpose",
                message="web_search results may only be used for draft enrichment",
            )

        # 1. Feature gate check.
        if not is_m9_subfeature_enabled(self.settings, "runbook_web_search"):
            AgentMetricsCollector.record_web_search_request(
                status="disabled", reason="feature_disabled"
            )
            return WebContextResult(status="disabled", purpose=purpose)

        # 2. Provider must not be disabled.
        provider_name = self.settings.runbook_web_search_provider.strip().lower()
        if provider_name == "disabled":
            AgentMetricsCollector.record_web_search_request(
                status="config_error", reason="provider_disabled"
            )
            return WebContextResult(
                status="config_error",
                purpose=purpose,
                error_message="web_search provider is disabled",
            )
        if provider_name not in SUPPORTED_WEB_SEARCH_PROVIDERS:
            AgentMetricsCollector.record_web_search_request(
                status="config_error", reason="unsupported_provider"
            )
            return WebContextResult(
                status="config_error",
                purpose=purpose,
                error_message=f"unsupported web_search provider: {provider_name}",
            )

        # 3. Production requires allowed domains.
        if self.settings.app_env == "production":
            allowed = self.settings.runbook_web_search_allowed_domains.strip()
            if not allowed:
                return self._blocked(
                    purpose=purpose,
                    reason="production_allowlist_required",
                    message="web_search requires allowed domains in production",
                )

        # 4. Redact query.
        redacted_query = redact_text(query).redacted_text

        # 5. Execute search.
        provider = self._provider or build_web_search_provider(self.settings)
        try:
            response = provider.search(redacted_query)
        except Exception:
            logger.warning("Web search provider failed", exc_info=True)
            AgentMetricsCollector.record_web_search_request(
                status="degraded", reason="provider_exception"
            )
            return WebContextResult(
                status="degraded",
                purpose=purpose,
                error_message="web_search provider exception",
            )

        if response.status != "ok":
            AgentMetricsCollector.record_web_search_request(
                status="degraded", reason="provider_degraded"
            )
            return WebContextResult(
                status="degraded",
                purpose=purpose,
                error_message=response.error_message or "web_search returned no results",
            )

        # 6. Validate result URLs for safety.
        url_validator = self._build_url_validator(provider_name)
        safe_results: list[WebSearchResult] = []
        for item in response.results:
            urls_to_validate = [item.original_url, *item.redirect_chain, item.final_url]
            validation_errors = [
                validation.reason or "unsafe URL"
                for validation in (url_validator.validate(url) for url in urls_to_validate)
                if not validation.is_safe
            ]
            if len(item.redirect_chain) > self.settings.runbook_web_search_max_redirects:
                validation_errors.append("redirect chain exceeds configured limit")
            if validation_errors:
                reason = validation_errors[0]
                logger.warning(
                    "Blocked unsafe URL in web search result: %s (%s)",
                    _safe_url_summary(item.final_url),
                    reason,
                )
                AgentMetricsCollector.record_web_search_blocked(
                    reason=_metric_reason(reason)
                )
                continue
            safe_results.append(WebSearchResult(
                title=item.title,
                original_url=item.original_url,
                final_url=item.final_url,
                snippet=_limit_text_bytes(
                    item.snippet,
                    min(500, self.settings.runbook_web_search_max_content_bytes),
                ),
                content_hash=item.content_hash,
                provider=item.provider,
                redaction_version=item.redaction_version,
                retrieved_at=item.retrieved_at or datetime.now(UTC).isoformat(),
            ))

        if not safe_results:
            return self._blocked(
                purpose=purpose,
                reason="all_results_blocked",
                message="all web_search results were blocked by safety rules",
            )

        AgentMetricsCollector.record_web_search_request(status="ok")
        return WebContextResult(
            status="ok",
            purpose=purpose,
            results=safe_results,
            query_redacted=redacted_query[:200],
        )

    def _build_url_validator(self, provider_name: str) -> BackendUrlSafetyValidator:
        dns_resolver = self._dns_resolver
        if dns_resolver is None and provider_name == "fake":
            dns_resolver = _fake_public_dns_resolver
        return BackendUrlSafetyValidator(
            app_env=self.settings.app_env,
            blocked_domain_patterns=_csv(self.settings.runbook_web_search_blocked_domains),
            allowed_domain_patterns=_csv(self.settings.runbook_web_search_allowed_domains),
            require_https=self.settings.runbook_web_search_require_https,
            strict_private_networks=True,
            block_cluster_internal_domains=True,
            resolve_dns=True,
            dns_resolver=dns_resolver,
        )

    @staticmethod
    def _blocked(*, purpose: str, reason: str, message: str) -> WebContextResult:
        AgentMetricsCollector.record_web_search_request(status="blocked", reason=reason)
        AgentMetricsCollector.record_web_search_blocked(reason=reason)
        return WebContextResult(
            status="blocked",
            purpose=purpose,
            error_message=message,
        )


def _csv(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def _fake_public_dns_resolver(_hostname: str) -> list[str]:
    return ["93.184.216.34"]


def _limit_text_bytes(text: str, max_bytes: int) -> str:
    raw = text.encode("utf-8")[:max_bytes]
    return raw.decode("utf-8", errors="ignore")


def _metric_reason(reason: str) -> str:
    normalized = reason.lower()
    if normalized == "https is required":
        return "https_required"
    if normalized == "url credentials are not allowed":
        return "url_credentials"
    if normalized.startswith("scheme "):
        return "scheme_not_allowed"
    if normalized.startswith("host ") and " is blocked" in normalized:
        return "blocked_domain"
    if normalized.startswith("host ") and "metadata endpoint" in normalized:
        return "metadata_endpoint"
    if normalized.startswith("host ") and "cluster-internal domain" in normalized:
        return "cluster_internal_domain"
    if normalized.startswith("host ") and "blocked in production" in normalized:
        return "blocked_host"
    if normalized.startswith("host ") and "not allowlisted" in normalized:
        return "not_allowlisted"
    if normalized.startswith("ip ") and "blocked network" in normalized:
        return "private_ip"
    if normalized.startswith("dns resolution failed"):
        return "dns_resolution_failed"
    if normalized.startswith("dns for host "):
        return "dns_resolved_private_ip"
    if normalized.startswith("redirect chain exceeds"):
        return "redirect_limit"
    if normalized == "url is empty":
        return "empty_url"
    if normalized == "url parsing failed":
        return "url_parse_failed"
    if normalized == "no hostname in url":
        return "missing_hostname"
    return "unsafe_url"


def _safe_url_summary(url: str) -> str:
    try:
        parsed = urlparse(url)
        if not parsed.scheme or not parsed.hostname:
            return "<invalid-url>"
        port = f":{parsed.port}" if parsed.port else ""
        return f"{parsed.scheme}://{parsed.hostname}{port}"
    except Exception:
        return "<invalid-url>"
