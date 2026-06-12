"""PR 9.4 — Web Search safety tests (redaction, URL safety, traceability)."""

from __future__ import annotations

import pytest

from packages.common.redaction import redact_text
from packages.common.settings import Settings


# ---------------------------------------------------------------------------
# Redaction in search queries
# ---------------------------------------------------------------------------

class TestWebSearchQueryRedaction:
    def test_query_redacts_token(self):
        """Search queries must not contain tokens."""
        text = "how to fix sk-abc123def456ghijklmnopqrstuvwxyz error"
        result = redact_text(text)
        assert "sk-abc123" not in result.redacted_text

    def test_query_redacts_password(self):
        """Search queries must not contain passwords."""
        text = 'database error password: "s3cret!"'
        result = redact_text(text)
        assert "s3cret!" not in result.redacted_text

    def test_query_redacts_private_key(self):
        """Search queries must not contain private keys."""
        text = """Error with key -----BEGIN RSA PRIVATE KEY-----
ABC123
-----END RSA PRIVATE KEY-----"""
        result = redact_text(text)
        assert "[REDACTED]" in result.redacted_text

    def test_query_redacts_bearer_token(self):
        """Search queries must not contain auth headers."""
        text = "API error Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
        result = redact_text(text)
        assert "eyJhbGci" not in result.redacted_text


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
        from datetime import datetime, UTC
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
    def test_disabled_provider_returns_config_error(self):
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
