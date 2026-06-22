"""Runbook Web Context Builder — M9 PR 9.4.

Orchestrates safe web search for runbook draft enrichment. Queries are redacted
before sending, URLs are validated for safety, and results are traceable with
source URLs, final URLs, content hashes, and retrieval timestamps.

Results are evidence for draft review only — never auto-published.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from time import perf_counter
from typing import Protocol

from packages.common.backend_url_safety import BackendUrlSafetyValidator
from packages.common.feature_flags import is_m9_subfeature_enabled
from packages.common.metrics import AgentMetricsCollector
from packages.common.redaction import RedactionResult, redact_text
from packages.common.settings import Settings
from packages.rag.web_search_provider import (
    SUPPORTED_WEB_SEARCH_PROVIDERS,
    WebSearchProvider,
    build_web_search_provider,
)

logger = logging.getLogger(__name__)

_WEB_CONTEXT_CACHE_VERSION = "webctx:v1"
_WEB_CONTEXT_CACHE_REDACTION_VERSION = "m9-9.4-cache-1"
_MEMORY_WEB_CONTEXT_CACHE: dict[str, tuple[float, str]] = {}


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
        cache: WebContextCacheBackend | None = None,
    ) -> None:
        self.settings = settings or Settings()
        self._provider = provider
        self._dns_resolver = dns_resolver
        self._cache = cache

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
        started_at = perf_counter()
        provider_name = self.settings.runbook_web_search_provider.strip().lower()
        query_redaction_count = 0

        if purpose != "draft_enrichment":
            return self._blocked(
                purpose=purpose,
                provider=provider_name,
                reason="unsupported_purpose",
                message="web_search results may only be used for draft enrichment",
                started_at=started_at,
            )

        # 1. Feature gate check.
        if not is_m9_subfeature_enabled(self.settings, "runbook_web_search"):
            AgentMetricsCollector.record_web_search_observation(
                provider=provider_name,
                status="disabled",
                reason="feature_disabled",
                duration_seconds=_elapsed_since(started_at),
            )
            return WebContextResult(status="disabled", purpose=purpose)

        # 2. Provider must not be disabled.
        if provider_name == "disabled":
            AgentMetricsCollector.record_web_search_observation(
                provider=provider_name,
                status="config_error",
                reason="provider_disabled",
                duration_seconds=_elapsed_since(started_at),
            )
            return WebContextResult(
                status="config_error",
                purpose=purpose,
                error_message="web_search provider is disabled",
            )
        if provider_name not in SUPPORTED_WEB_SEARCH_PROVIDERS:
            AgentMetricsCollector.record_web_search_observation(
                provider=provider_name,
                status="config_error",
                reason="unsupported_provider",
                duration_seconds=_elapsed_since(started_at),
            )
            return WebContextResult(
                status="config_error",
                purpose=purpose,
                error_message="unsupported web_search provider",
            )

        # 3. Production requires allowed domains.
        if self.settings.app_env == "production":
            allowed = self.settings.runbook_web_search_allowed_domains.strip()
            if not allowed:
                return self._blocked(
                    purpose=purpose,
                    provider=provider_name,
                    reason="production_allowlist_required",
                    message="web_search requires allowed domains in production",
                    started_at=started_at,
                )

        # 4. Redact query.
        redaction = _redact_web_search_text(query)
        redacted_query = redaction.redacted_text
        query_redaction_count = redaction.redaction_count
        cache_status = "not_applicable"
        cache = self._cache_backend()
        cache_key = ""
        url_validator = self._build_url_validator(provider_name)
        if cache is not None:
            cache_key = _web_context_cache_key(
                self.settings,
                provider=provider_name,
                purpose=purpose,
                redacted_query=redacted_query,
            )
            cache_status, cached_results = _safe_cache_get(cache, cache_key)
            if cache_status == "hit":
                safe_cached = _validate_cached_results(cached_results, url_validator)
                if safe_cached is not None:
                    AgentMetricsCollector.record_web_search_observation(
                        provider=provider_name,
                        status="ok",
                        duration_seconds=_elapsed_since(started_at),
                        result_count=len(safe_cached),
                        query_redaction_count=query_redaction_count,
                        cache_status="hit",
                    )
                    return WebContextResult(
                        status="ok",
                        purpose=purpose,
                        results=safe_cached,
                        query_redacted=redacted_query[:200],
                    )
                cache_status = "unknown"

        # 5. Execute search.
        provider = self._provider or build_web_search_provider(self.settings)
        try:
            response = provider.search(redacted_query)
        except Exception:
            logger.warning("Web search provider failed")
            AgentMetricsCollector.record_web_search_observation(
                provider=provider_name,
                status="degraded",
                reason="provider_exception",
                duration_seconds=_elapsed_since(started_at),
                query_redaction_count=query_redaction_count,
                cache_status=cache_status,
            )
            return WebContextResult(
                status="degraded",
                purpose=purpose,
                error_message="web_search provider exception",
            )

        if response.status != "ok":
            AgentMetricsCollector.record_web_search_observation(
                provider=provider_name,
                status="degraded",
                reason="provider_degraded",
                duration_seconds=_elapsed_since(started_at),
                query_redaction_count=query_redaction_count,
                cache_status=cache_status,
            )
            return WebContextResult(
                status="degraded",
                purpose=purpose,
                error_message=_safe_diagnostic_message(
                    response.error_message,
                    fallback="web_search returned no results",
                ),
            )

        # 6. Validate result URLs for safety.
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
                metric_reason = _metric_reason(reason)
                logger.warning(
                    "Blocked unsafe URL in web search result: reason_code=%s",
                    metric_reason,
                )
                AgentMetricsCollector.record_web_search_blocked(
                    provider=provider_name,
                    reason=metric_reason,
                )
                continue
            redacted_title = _redact_web_search_text(item.title).redacted_text
            redacted_snippet = _redact_web_search_text(item.snippet).redacted_text
            safe_results.append(WebSearchResult(
                title=redacted_title,
                original_url=item.original_url,
                final_url=item.final_url,
                snippet=_limit_text_bytes(
                    redacted_snippet,
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
                provider=provider_name,
                reason="all_results_blocked",
                message="all web_search results were blocked by safety rules",
                started_at=started_at,
                query_redaction_count=query_redaction_count,
                cache_status=cache_status,
            )

        if cache is not None and cache_key:
            cache_status = _safe_cache_set(
                cache,
                cache_key,
                safe_results,
                ttl_seconds=self.settings.runbook_web_search_cache_ttl_seconds,
                previous_status=cache_status,
            )

        AgentMetricsCollector.record_web_search_observation(
            provider=provider_name,
            status="ok",
            duration_seconds=_elapsed_since(started_at),
            result_count=len(safe_results),
            query_redaction_count=query_redaction_count,
            cache_status=cache_status,
        )
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

    def _cache_backend(self) -> WebContextCacheBackend | None:
        if not self.settings.runbook_web_search_cache_enabled:
            return None
        if self._cache is not None:
            return self._cache
        return RedisOrMemoryWebContextCache(self.settings.redis_url)

    @staticmethod
    def _blocked(
        *,
        purpose: str,
        provider: str,
        reason: str,
        message: str,
        started_at: float,
        query_redaction_count: int = 0,
        cache_status: str = "not_applicable",
    ) -> WebContextResult:
        AgentMetricsCollector.record_web_search_observation(
            provider=provider,
            status="blocked",
            reason=reason,
            duration_seconds=_elapsed_since(started_at),
            query_redaction_count=query_redaction_count,
            cache_status=cache_status,
        )
        AgentMetricsCollector.record_web_search_blocked(
            provider=provider,
            reason=reason,
        )
        return WebContextResult(
            status="blocked",
            purpose=purpose,
            error_message=message,
        )


class WebContextCacheBackend(Protocol):
    """Minimal cache port for safe Web context results.

    Backends store JSON payloads containing only validated traceability fields:
    title, source/final URLs, snippet, content hash, provider, redaction version,
    and retrieval time. Keys are opaque hashes built from redacted query and
    policy/budget fields, never from raw query text, URL paths, hosts, or secrets.
    """

    def get(self, key: str) -> str | None:
        """Return serialized cache payload, or None for miss."""

    def setex(self, key: str, ttl_seconds: int, value: str) -> None:
        """Store serialized cache payload with TTL."""


class RedisOrMemoryWebContextCache:
    """Redis-backed Web context cache with `memory://` test fallback.

    Rollback path: set `RUNBOOK_WEB_SEARCH_CACHE_ENABLED=false`. The cache has no
    database schema and stores only bounded, previously validated result fields.
    """

    def __init__(self, redis_url: str) -> None:
        self._memory = redis_url.startswith("memory://")
        self._redis = None
        if not self._memory:
            import redis

            self._redis = redis.Redis.from_url(
                redis_url,
                socket_connect_timeout=0.2,
                socket_timeout=0.2,
            )

    def get(self, key: str) -> str | None:
        if self._memory:
            record = _MEMORY_WEB_CONTEXT_CACHE.get(key)
            if record is None:
                return None
            expires_at, value = record
            if expires_at <= time.time():
                _MEMORY_WEB_CONTEXT_CACHE.pop(key, None)
                return None
            return value
        if self._redis is None:
            return None
        raw = self._redis.get(key)
        if raw is None:
            return None
        if isinstance(raw, bytes):
            return raw.decode("utf-8", errors="ignore")
        return str(raw)

    def setex(self, key: str, ttl_seconds: int, value: str) -> None:
        ttl = max(1, int(ttl_seconds))
        if self._memory:
            _MEMORY_WEB_CONTEXT_CACHE[key] = (time.time() + ttl, value)
            return
        if self._redis is not None:
            self._redis.setex(key, ttl, value)


def _csv(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def _fake_public_dns_resolver(_hostname: str) -> list[str]:
    return ["93.184.216.34"]


def _limit_text_bytes(text: str, max_bytes: int) -> str:
    raw = text.encode("utf-8")[:max_bytes]
    return raw.decode("utf-8", errors="ignore")


def _safe_diagnostic_message(message: str | None, *, fallback: str) -> str:
    if not message:
        return fallback
    redacted = _redact_web_search_text(message).redacted_text.strip()
    if not redacted:
        return fallback
    return _limit_text_bytes(redacted, 300)


_WEB_SEARCH_PRE_REDACTION_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "web_search_private_ip_url",
        re.compile(
            r"""\b(?:https?://)?(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3})(?::\d+)?(?:/[^\s"')\]}>]*)?""",
            re.IGNORECASE,
        ),
    ),
    (
        "web_search_internal_host",
        re.compile(
            r"""\b(?:[A-Za-z0-9-]+\.)+(?:svc(?:\.cluster\.local)?|cluster\.local|internal|local)(?::\d+)?(?:/[^\s"')\]}>]*)?""",
            re.IGNORECASE,
        ),
    ),
    (
        "web_search_url_path",
        re.compile(r"""\bhttps?://[^/\s"')\]}>]+/[^\s"')\]}>]*""", re.IGNORECASE),
    ),
)

_WEB_SEARCH_POST_REDACTION_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "web_search_redacted_url_path",
        re.compile(r"""\[REDACTED\](?::\d+)?/[^\s"')\]}>]*"""),
    ),
    (
        "web_search_path_token",
        re.compile(r"""(?<![\w:])/[A-Za-z0-9][A-Za-z0-9._~!$&()*+,;=:@%/-]*"""),
    ),
)


def _redact_web_search_text(text: str) -> RedactionResult:
    """Redact secrets plus Web-search-specific topology and URL path details."""
    result, count, redaction_types = _apply_web_search_redaction_rules(
        text,
        _WEB_SEARCH_PRE_REDACTION_RULES,
    )
    base = redact_text(result)
    count += base.redaction_count
    redaction_types.extend(base.redaction_types)
    result, post_count, post_types = _apply_web_search_redaction_rules(
        base.redacted_text,
        _WEB_SEARCH_POST_REDACTION_RULES,
    )
    count += post_count
    redaction_types.extend(post_types)
    return RedactionResult(
        redacted_text=result,
        redaction_count=count,
        redaction_types=redaction_types,
    )


def _apply_web_search_redaction_rules(
    text: str,
    rules: tuple[tuple[str, re.Pattern[str]], ...],
) -> tuple[str, int, list[str]]:
    result = text
    count = 0
    redaction_types: list[str] = []
    for name, pattern in rules:
        matches = list(pattern.finditer(result))
        if not matches:
            continue
        result = pattern.sub("[REDACTED]", result)
        count += len(matches)
        redaction_types.append(name)
    return result, count, redaction_types


def _elapsed_since(started_at: float) -> float:
    return max(0.0, perf_counter() - started_at)


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


def _web_context_cache_key(
    settings: Settings,
    *,
    provider: str,
    purpose: str,
    redacted_query: str,
) -> str:
    ttl = max(1, int(settings.runbook_web_search_cache_ttl_seconds))
    parts = {
        "version": _WEB_CONTEXT_CACHE_VERSION,
        "provider": provider,
        "purpose": purpose,
        "query_hash": _sha256(redacted_query),
        "allowed_policy_hash": _sha256(
            _normalized_csv(settings.runbook_web_search_allowed_domains)
        ),
        "blocked_policy_hash": _sha256(
            _normalized_csv(settings.runbook_web_search_blocked_domains)
        ),
        "require_https": settings.runbook_web_search_require_https,
        "max_redirects": settings.runbook_web_search_max_redirects,
        "max_results": settings.runbook_web_search_max_results,
        "max_content_bytes": settings.runbook_web_search_max_content_bytes,
        "recency_bucket": int(time.time() // ttl),
        "redaction_version": _WEB_CONTEXT_CACHE_REDACTION_VERSION,
    }
    digest = _sha256(json.dumps(parts, sort_keys=True, separators=(",", ":")))
    return f"{_WEB_CONTEXT_CACHE_VERSION}:{digest}"


def _safe_cache_get(
    cache: WebContextCacheBackend,
    key: str,
) -> tuple[str, list[WebSearchResult]]:
    try:
        raw = cache.get(key)
    except Exception:
        logger.warning("Web search cache read failed")
        return "unknown", []
    if not raw:
        return "miss", []
    try:
        return "hit", _deserialize_cache_results(raw)
    except Exception:
        logger.warning("Web search cache payload ignored")
        return "unknown", []


def _safe_cache_set(
    cache: WebContextCacheBackend,
    key: str,
    results: list[WebSearchResult],
    *,
    ttl_seconds: int,
    previous_status: str,
) -> str:
    try:
        cache.setex(key, ttl_seconds, _serialize_cache_results(results))
    except Exception:
        logger.warning("Web search cache write failed")
        return "unknown"
    return previous_status if previous_status in {"hit", "miss"} else "miss"


def _serialize_cache_results(results: list[WebSearchResult]) -> str:
    return json.dumps(
        {
            "version": 1,
            "results": [
                {
                    "title": result.title,
                    "original_url": result.original_url,
                    "final_url": result.final_url,
                    "snippet": result.snippet,
                    "content_hash": result.content_hash,
                    "provider": result.provider,
                    "redaction_version": result.redaction_version,
                    "retrieved_at": result.retrieved_at,
                }
                for result in results
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _deserialize_cache_results(raw: str) -> list[WebSearchResult]:
    payload = json.loads(raw)
    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise ValueError("unsupported cache payload")
    items = payload.get("results")
    if not isinstance(items, list):
        raise ValueError("invalid cache payload")
    results: list[WebSearchResult] = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("invalid cache result")
        fields = {
            key: item.get(key)
            for key in (
                "title",
                "original_url",
                "final_url",
                "snippet",
                "content_hash",
                "provider",
                "redaction_version",
                "retrieved_at",
            )
        }
        if not all(isinstance(value, str) for value in fields.values()):
            raise ValueError("invalid cache result fields")
        results.append(WebSearchResult(**fields))  # type: ignore[arg-type]
    return results


def _validate_cached_results(
    results: list[WebSearchResult],
    url_validator: BackendUrlSafetyValidator,
) -> list[WebSearchResult] | None:
    for item in results:
        for url in (item.original_url, item.final_url):
            validation = url_validator.validate(url)
            if not validation.is_safe:
                logger.warning("Web search cached result failed safety validation")
                return None
    return results


def _normalized_csv(value: str) -> str:
    return ",".join(sorted(part.lower() for part in _csv(value)))


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
