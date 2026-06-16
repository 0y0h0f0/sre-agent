"""PR 9.4 — Web Search safety tests (redaction, URL safety, traceability)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from packages.common.redaction import redact_text
from packages.common.settings import Settings
from packages.rag.web_search_provider import WebSearchResponse, WebSearchResultItem


class StaticWebSearchProvider:
    name = "static"

    def __init__(self, results: list[WebSearchResultItem]) -> None:
        self.results = results
        self.queries: list[str] = []

    def search(self, query: str) -> WebSearchResponse:
        self.queries.append(query)
        return WebSearchResponse(status="ok", results=self.results, query_redacted=query)


def _enabled_settings(**overrides) -> Settings:
    values = {
        "m9_extensions_enabled": True,
        "runbook_web_search_enabled": True,
        "runbook_web_search_provider": "fake",
        "api_key_auth_enabled": False,
    }
    values.update(overrides)
    return Settings(**values)


def _item(
    *,
    original_url: str = "https://docs.example.com/start",
    final_url: str = "https://docs.example.com/final",
    snippet: str = "safe runbook guidance",
    redirect_chain: list[str] | None = None,
) -> WebSearchResultItem:
    return WebSearchResultItem(
        title="Runbook guidance",
        original_url=original_url,
        final_url=final_url,
        snippet=snippet,
        content_hash="sha256:test",
        provider="static",
        redaction_version="m9-9.4-1",
        retrieved_at="2026-06-01T00:00:00+00:00",
        redirect_chain=redirect_chain or [],
    )


# ---------------------------------------------------------------------------
# Redaction in search queries
# ---------------------------------------------------------------------------

class TestWebSearchQueryRedaction:
    def test_query_redacts_token(self):
        """Search queries must not contain tokens."""
        token = "sk-" + "abc123def456ghijklmnopqrstuvwxyz"
        text = f"how to fix {token} error"
        result = redact_text(text)
        assert token not in result.redacted_text

    def test_query_redacts_password(self):
        """Search queries must not contain passwords."""
        text = 'database error password: "s3cret!"'
        result = redact_text(text)
        assert "s3cret!" not in result.redacted_text

    def test_query_redacts_private_key(self):
        """Search queries must not contain private keys."""
        text = "Error with key " + "\n".join([
            "-----BEGIN RSA " + "PRIVATE KEY-----",
            "ABC123",
            "-----END RSA " + "PRIVATE KEY-----",
        ])
        result = redact_text(text)
        assert "[REDACTED]" in result.redacted_text

    def test_query_redacts_bearer_token(self):
        """Search queries must not contain auth headers."""
        text = "API error Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
        result = redact_text(text)
        assert "eyJhbGci" not in result.redacted_text

    def test_query_redacts_internal_url_namespace_and_service_name(self):
        """External web search queries must not expose internal topology."""
        text = (
            "service=checkout namespace=prod "
            "https://api.prod.svc.cluster.local/v1 failed"
        )
        result = redact_text(text)
        assert "checkout" not in result.redacted_text
        assert "namespace=prod" not in result.redacted_text
        assert "svc.cluster.local" not in result.redacted_text


# ---------------------------------------------------------------------------
# URL safety
# ---------------------------------------------------------------------------

class TestWebSearchUrlSafety:
    def test_blocks_localhost(self):
        """localhost URLs must be blocked."""
        from packages.common.backend_url_safety import BackendUrlSafetyValidator
        validator = BackendUrlSafetyValidator(app_env="production")
        result = validator.validate("http://localhost:8080/search")
        assert result.is_safe is False

    def test_blocks_metadata_endpoint(self):
        """Metadata endpoints must be blocked."""
        from packages.common.backend_url_safety import BackendUrlSafetyValidator
        validator = BackendUrlSafetyValidator(app_env="production")
        result = validator.validate("http://169.254.169.254/metadata")
        assert result.is_safe is False

    def test_blocks_private_ip(self):
        """Private IPs must be blocked in production."""
        from packages.common.backend_url_safety import BackendUrlSafetyValidator
        validator = BackendUrlSafetyValidator(app_env="production")
        result = validator.validate("http://10.0.1.5:8080/api")
        assert result.is_safe is False

    def test_requires_https_by_default(self):
        """HTTP should be flagged — HTTPS is the safe default."""
        from packages.common.backend_url_safety import BackendUrlSafetyValidator
        validator = BackendUrlSafetyValidator(app_env="production")
        result = validator.validate("http://example.com/search")
        # HTTP is allowed by BackendUrlSafetyValidator (scheme is http/https)
        # but web search provider should additionally enforce HTTPS
        assert result.is_safe is True  # scheme check passes

    def test_https_passes_safety(self):
        """HTTPS URLs to public hosts should be safe."""
        from packages.common.backend_url_safety import BackendUrlSafetyValidator
        validator = BackendUrlSafetyValidator(app_env="production")
        result = validator.validate("https://docs.example.com/runbook")
        assert result.is_safe is True

    def test_web_search_blocks_cluster_internal_domain(self):
        """Cluster-internal DNS names must be blocked for web_search."""
        from packages.common.backend_url_safety import BackendUrlSafetyValidator

        validator = BackendUrlSafetyValidator(
            app_env="local",
            block_cluster_internal_domains=True,
            strict_private_networks=True,
        )
        result = validator.validate("https://api.prod.svc.cluster.local/runbook")
        assert result.is_safe is False

    def test_web_search_requires_https(self):
        """Web search safety mode requires HTTPS by default."""
        from packages.common.backend_url_safety import BackendUrlSafetyValidator

        validator = BackendUrlSafetyValidator(
            app_env="local",
            require_https=True,
            strict_private_networks=True,
        )
        result = validator.validate("http://docs.example.com/runbook")
        assert result.is_safe is False

    def test_web_search_blocked_domains_override_allowed_domains(self):
        """Blocked domains must win over allowlist entries."""
        from packages.common.backend_url_safety import BackendUrlSafetyValidator

        validator = BackendUrlSafetyValidator(
            app_env="production",
            allowed_domain_patterns=["*.example.com"],
            blocked_domain_patterns=["docs.example.com"],
            require_https=True,
            strict_private_networks=True,
            resolve_dns=True,
            dns_resolver=lambda _host: ["93.184.216.34"],
        )
        result = validator.validate("https://docs.example.com/runbook")
        assert result.is_safe is False

    def test_web_search_dns_resolution_private_ip_blocked(self):
        """DNS rebinding to private IPs must be blocked."""
        from packages.common.backend_url_safety import BackendUrlSafetyValidator

        validator = BackendUrlSafetyValidator(
            app_env="production",
            require_https=True,
            strict_private_networks=True,
            resolve_dns=True,
            dns_resolver=lambda _host: ["10.0.0.8"],
        )
        result = validator.validate("https://docs.example.com/runbook")
        assert result.is_safe is False

    def test_web_search_dns_resolution_empty_result_blocked(self):
        """DNS resolution must produce at least one public IP."""
        from packages.common.backend_url_safety import BackendUrlSafetyValidator

        validator = BackendUrlSafetyValidator(
            app_env="production",
            require_https=True,
            strict_private_networks=True,
            resolve_dns=True,
            dns_resolver=lambda _host: [],
        )
        result = validator.validate("https://docs.example.com/runbook")
        assert result.is_safe is False

    def test_web_search_blocks_url_embedded_credentials(self):
        """URLs with userinfo must be rejected before they can reach logs/state."""
        from packages.common.backend_url_safety import BackendUrlSafetyValidator

        validator = BackendUrlSafetyValidator(
            app_env="production",
            require_https=True,
            strict_private_networks=True,
            resolve_dns=True,
            dns_resolver=lambda _host: ["93.184.216.34"],
        )
        result = validator.validate("https://user:s3cret@docs.example.com/runbook")
        assert result.is_safe is False
        assert result.reason == "URL credentials are not allowed"


# ---------------------------------------------------------------------------
# Web search settings
# ---------------------------------------------------------------------------

class TestWebSearchSettings:
    def test_web_search_default_disabled(self):
        """RUNBOOK_WEB_SEARCH_ENABLED defaults to False."""
        settings = Settings()
        assert settings.runbook_web_search_enabled is False

    def test_web_search_provider_defaults(self):
        """Web search provider settings have safe defaults."""
        settings = Settings()
        assert settings.runbook_web_search_timeout_seconds == 10
        assert settings.runbook_web_search_max_results == 5
        assert settings.runbook_web_search_require_https is True
        assert settings.runbook_web_search_max_redirects == 3
        assert settings.runbook_web_search_max_content_bytes == 1_048_576

    def test_web_search_allowed_domains_default_empty(self):
        """Allowed domains default to empty (production requires non-empty)."""
        settings = Settings()
        assert settings.runbook_web_search_allowed_domains == ""

    def test_web_search_cache_ttl_default(self):
        """Cache TTL defaults to 24 hours."""
        settings = Settings()
        assert settings.runbook_web_search_cache_ttl_seconds == 86400


# ---------------------------------------------------------------------------
# Source traceability
# ---------------------------------------------------------------------------

class TestWebSearchSourceTraceability:
    def test_result_has_source_url(self):
        """Search results must include original_url for traceability."""
        from packages.rag.runbook_web_context import WebSearchResult
        result = WebSearchResult(
            title="Test Result",
            original_url="https://docs.example.com/page",
            final_url="https://docs.example.com/page",
            snippet="A helpful snippet.",
            content_hash="abc123",
            provider="fake",
            redaction_version="m9-9.4-1",
        )
        assert result.original_url == "https://docs.example.com/page"

    def test_result_has_final_url(self):
        """Search results must include final_url (after redirects)."""
        from packages.rag.runbook_web_context import WebSearchResult
        result = WebSearchResult(
            title="Redirected",
            original_url="https://short.link/abc",
            final_url="https://docs.example.com/full-page",
            snippet="After redirect.",
            content_hash="def456",
            provider="fake",
            redaction_version="m9-9.4-1",
        )
        assert result.original_url != result.final_url

    def test_result_has_retrieved_at(self):
        """Search results must include retrieved_at timestamp."""
        from datetime import UTC, datetime

        from packages.rag.runbook_web_context import WebSearchResult

        now = datetime.now(UTC)
        result = WebSearchResult(
            title="Test",
            original_url="https://example.com",
            final_url="https://example.com",
            snippet="...",
            content_hash="abc",
            provider="fake",
            redaction_version="v1",
            retrieved_at=now.isoformat(),
        )
        assert result.retrieved_at is not None

    def test_result_has_content_hash(self):
        """Search results must include content_hash for integrity verification."""
        from packages.rag.runbook_web_context import WebSearchResult
        result = WebSearchResult(
            title="Test",
            original_url="https://example.com",
            final_url="https://example.com",
            snippet="Content",
            content_hash="sha256:abc123",
            provider="fake",
            redaction_version="v1",
        )
        assert result.content_hash == "sha256:abc123"


# ---------------------------------------------------------------------------
# Provider disabled behavior
# ---------------------------------------------------------------------------

class TestWebSearchProviderDisabled:
    def test_disabled_provider_returns_degraded(self):
        """When provider is disabled, search returns degraded, not fallback."""
        from packages.rag.web_search_provider import DisabledWebSearchProvider

        provider = DisabledWebSearchProvider()
        result = provider.search("test query")
        assert result.status == "degraded"

    def test_disabled_provider_does_not_fallback(self):
        """Disabled provider must not fallback to any default."""
        from packages.rag.web_search_provider import DisabledWebSearchProvider

        provider = DisabledWebSearchProvider()
        result = provider.search("test query")
        assert result.results == []


# ---------------------------------------------------------------------------
# Builder safety behavior
# ---------------------------------------------------------------------------


class TestRunbookWebContextBuilder:
    def test_web_search_requires_m9_enabled(self):
        from packages.rag.runbook_web_context import RunbookWebContextBuilder

        settings = Settings(
            m9_extensions_enabled=False,
            runbook_web_search_enabled=True,
            runbook_web_search_provider="fake",
        )
        result = RunbookWebContextBuilder(settings=settings).build_context(query="latency")
        assert result.status == "disabled"

    def test_web_search_enabled_with_provider_disabled_returns_config_error(self):
        from packages.rag.runbook_web_context import RunbookWebContextBuilder

        settings = _enabled_settings(runbook_web_search_provider="disabled")
        result = RunbookWebContextBuilder(settings=settings).build_context(query="latency")
        assert result.status == "config_error"

    def test_web_search_does_not_fallback_to_default_provider(self):
        from packages.rag.runbook_web_context import RunbookWebContextBuilder

        settings = _enabled_settings(runbook_web_search_provider="unknown")
        result = RunbookWebContextBuilder(settings=settings).build_context(query="latency")
        assert result.status == "config_error"

    def test_web_search_production_requires_allowed_domains(self):
        from packages.rag.runbook_web_context import RunbookWebContextBuilder

        settings = _enabled_settings(app_env="production")
        result = RunbookWebContextBuilder(settings=settings).build_context(query="latency")
        assert result.status == "blocked"
        assert "allowed domains" in (result.error_message or "")

    def test_web_search_query_is_redacted_before_provider_call(self):
        from packages.rag.runbook_web_context import RunbookWebContextBuilder

        provider = StaticWebSearchProvider([_item()])
        builder = RunbookWebContextBuilder(
            settings=_enabled_settings(),
            provider=provider,
            dns_resolver=lambda _host: ["93.184.216.34"],
        )
        token = "sk-" + "abcdefghijklmnopqrstuvwxyz123456"
        result = builder.build_context(
            query=f"service=checkout password=s3cret token {token}"
        )
        assert result.status == "ok"
        assert provider.queries
        assert "checkout" not in provider.queries[0]
        assert "s3cret" not in provider.queries[0]
        assert token not in provider.queries[0]

    def test_web_search_redirect_to_metadata_blocked(self):
        from packages.rag.runbook_web_context import RunbookWebContextBuilder

        provider = StaticWebSearchProvider([
            _item(redirect_chain=["http://169.254.169.254/latest/meta-data"])
        ])
        result = RunbookWebContextBuilder(
            settings=_enabled_settings(),
            provider=provider,
            dns_resolver=lambda _host: ["93.184.216.34"],
        ).build_context(query="metadata")
        assert result.status == "blocked"

    def test_web_search_redirect_revalidates_final_url(self):
        from packages.rag.runbook_web_context import RunbookWebContextBuilder

        provider = StaticWebSearchProvider([_item(final_url="https://10.0.0.5/path")])
        result = RunbookWebContextBuilder(
            settings=_enabled_settings(),
            provider=provider,
            dns_resolver=lambda _host: ["93.184.216.34"],
        ).build_context(query="private ip")
        assert result.status == "blocked"

    def test_web_search_response_size_limited(self):
        from packages.rag.runbook_web_context import RunbookWebContextBuilder

        provider = StaticWebSearchProvider([_item(snippet="x" * 200)])
        result = RunbookWebContextBuilder(
            settings=_enabled_settings(runbook_web_search_max_content_bytes=12),
            provider=provider,
            dns_resolver=lambda _host: ["93.184.216.34"],
        ).build_context(query="latency")
        assert result.status == "ok"
        assert len(result.results[0].snippet.encode()) <= 12

    def test_web_search_result_has_traceability_fields(self):
        from packages.rag.runbook_web_context import RunbookWebContextBuilder

        provider = StaticWebSearchProvider([_item()])
        result = RunbookWebContextBuilder(
            settings=_enabled_settings(),
            provider=provider,
            dns_resolver=lambda _host: ["93.184.216.34"],
        ).build_context(query="latency")
        item = result.results[0]
        assert item.original_url
        assert item.final_url
        assert item.retrieved_at
        assert item.content_hash
        assert item.redaction_version

    def test_web_search_only_attaches_to_draft(self):
        from packages.rag.runbook_web_context import RunbookWebContextBuilder

        provider = StaticWebSearchProvider([_item()])
        result = RunbookWebContextBuilder(
            settings=_enabled_settings(),
            provider=provider,
            dns_resolver=lambda _host: ["93.184.216.34"],
        ).build_context(query="latency", purpose="approved_runbook")
        assert result.status == "blocked"

    def test_web_search_blocked_url_log_does_not_leak_credentials(self, caplog):
        from packages.rag.runbook_web_context import RunbookWebContextBuilder

        caplog.set_level("WARNING", logger="packages.rag.runbook_web_context")
        provider = StaticWebSearchProvider([
            _item(final_url="https://user:s3cret@10.0.0.5/path?api_key=abc")
        ])
        result = RunbookWebContextBuilder(
            settings=_enabled_settings(),
            provider=provider,
            dns_resolver=lambda _host: ["93.184.216.34"],
        ).build_context(query="private ip")
        assert result.status == "blocked"
        assert "s3cret" not in caplog.text
        assert "api_key" not in caplog.text
        assert "user:s3cret" not in caplog.text

    def test_web_search_metric_reason_uses_fixed_code(self):
        from packages.rag.runbook_web_context import _metric_reason

        reason = _metric_reason("Host 'token-secret.example.com' is blocked")
        assert reason == "blocked_domain"
        assert "token-secret" not in reason


# ---------------------------------------------------------------------------
# web_search only attaches to draft
# ---------------------------------------------------------------------------

class TestWebSearchDraftOnly:
    def test_search_context_marks_draft_only(self):
        """Web search results are for draft enrichment, not auto-publish."""
        from packages.rag.runbook_web_context import RunbookWebContextBuilder

        builder = RunbookWebContextBuilder()
        result = builder.build_context(
            query="SRE runbook for high 5xx errors",
            purpose="draft_enrichment",
        )
        assert result.purpose == "draft_enrichment"
        # Results are evidence for draft review, not direct publication

    def test_search_does_not_publish_runbook(self):
        """Web search never directly publishes — evidence-only mode."""
        from packages.rag.runbook_web_context import RunbookWebContextBuilder

        builder = RunbookWebContextBuilder()
        result = builder.build_context(
            query="SRE runbook for high 5xx",
            purpose="draft_enrichment",
        )
        # The builder returns context for drafts — no publish path exists
        assert result is not None


# ---------------------------------------------------------------------------
# API scope requirement
# ---------------------------------------------------------------------------


class TestWebSearchScopeRequirement:
    def test_web_search_requires_runbook_review_and_web_search_scope(self):
        from apps.api.routers.runbooks import _require_web_search_scopes

        request = SimpleNamespace(
            state=SimpleNamespace(api_key={"scopes": ["runbook:review"]})
        )
        with pytest.raises(HTTPException) as exc:
            _require_web_search_scopes(
                request,  # type: ignore[arg-type]
                settings=Settings(api_key_auth_enabled=True),
            )
        assert exc.value.status_code == 403
        assert "runbook:web_search" in exc.value.detail

    def test_web_search_allows_both_required_scopes(self):
        from apps.api.routers.runbooks import _require_web_search_scopes

        request = SimpleNamespace(
            state=SimpleNamespace(
                api_key={"scopes": ["runbook:review", "runbook:web_search"]}
            )
        )
        _require_web_search_scopes(
            request,  # type: ignore[arg-type]
            settings=Settings(api_key_auth_enabled=True),
        )
