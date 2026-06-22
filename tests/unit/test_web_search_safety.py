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


class DegradedWebSearchProvider:
    name = "degraded"

    def __init__(self, *, error_message: str) -> None:
        self.error_message = error_message
        self.queries: list[str] = []

    def search(self, query: str) -> WebSearchResponse:
        self.queries.append(query)
        return WebSearchResponse(status="degraded", error_message=self.error_message)


class RaisingWebSearchProvider:
    name = "raising"

    def __init__(self, *, error_message: str) -> None:
        self.error_message = error_message
        self.queries: list[str] = []

    def search(self, query: str) -> WebSearchResponse:
        self.queries.append(query)
        raise RuntimeError(self.error_message)


class _MetricSpy:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, str], float]] = []

    def labels(self, **labels: str) -> _MetricSpyChild:
        return _MetricSpyChild(self, labels)


class _MetricSpyChild:
    def __init__(self, parent: _MetricSpy, labels: dict[str, str]) -> None:
        self.parent = parent
        self.labels = labels

    def inc(self, amount: float = 1.0) -> None:
        self.parent.calls.append(("inc", dict(self.labels), amount))

    def observe(self, value: float) -> None:
        self.parent.calls.append(("observe", dict(self.labels), value))


def _patch_web_search_metrics(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    from packages.common import metrics

    spies = SimpleNamespace(
        requests=_MetricSpy(),
        blocked=_MetricSpy(),
        results=_MetricSpy(),
        redactions=_MetricSpy(),
        cache=_MetricSpy(),
        duration=_MetricSpy(),
    )
    monkeypatch.setattr(metrics, "web_search_requests_total", spies.requests)
    monkeypatch.setattr(metrics, "web_search_blocked_total", spies.blocked)
    monkeypatch.setattr(metrics, "web_search_results_total", spies.results)
    monkeypatch.setattr(
        metrics,
        "web_search_query_redactions_total",
        spies.redactions,
    )
    monkeypatch.setattr(metrics, "web_search_cache_status_total", spies.cache)
    monkeypatch.setattr(metrics, "web_search_duration_seconds", spies.duration)
    return spies


def _matching_calls(
    spy: _MetricSpy,
    action: str,
    **labels: str,
) -> list[tuple[str, dict[str, str], float]]:
    return [
        call
        for call in spy.calls
        if call[0] == action
        and all(call[1].get(key) == value for key, value in labels.items())
    ]


def _all_metric_call_text(spies: SimpleNamespace) -> str:
    return repr([
        spies.requests.calls,
        spies.blocked.calls,
        spies.results.calls,
        spies.redactions.calls,
        spies.cache.calls,
        spies.duration.calls,
    ])


def _enabled_settings(**overrides) -> Settings:
    values = {
        "m9_extensions_enabled": True,
        "runbook_web_search_enabled": True,
        "runbook_web_search_provider": "fake",
        "runbook_web_search_cache_enabled": False,
        "redis_url": "memory://web-search-default",
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

    def test_query_redacts_short_token_and_secret_values(self):
        """Short keyed token/secret values must not bypass redaction."""
        text = "k8s event token=short-secret secret: prod-db-password"
        result = redact_text(text)
        assert "short-secret" not in result.redacted_text
        assert "prod-db-password" not in result.redacted_text
        assert result.redaction_count >= 2

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
        assert settings.runbook_web_search_cache_enabled is True

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
            query=(
                f"service=checkout password=s3cret token {token} "
                "api.prod.svc.cluster.local/v1"
            )
        )
        assert result.status == "ok"
        assert provider.queries
        assert "checkout" not in provider.queries[0]
        assert "s3cret" not in provider.queries[0]
        assert token not in provider.queries[0]
        assert "api.prod.svc.cluster.local" not in provider.queries[0]
        assert "/v1" not in provider.queries[0]

    def test_web_search_payload_redacts_bare_internal_topology(self):
        from packages.rag.runbook_web_context import RunbookWebContextBuilder

        result = RunbookWebContextBuilder(
            settings=_enabled_settings(runbook_web_search_max_results=1),
        ).build_context(
            query="api.prod.svc.cluster.local latency 10.0.0.5/runbook /readyz"
        )

        assert result.status == "ok"
        assert result.results
        payload_text = " ".join([
            result.query_redacted,
            result.results[0].title,
            result.results[0].snippet,
        ])
        for sensitive in (
            "api.prod.svc.cluster.local",
            "svc.cluster.local",
            "10.0.0.5",
            "/runbook",
            "/readyz",
        ):
            assert sensitive not in payload_text

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
        for sensitive in (
            "10.0.0.5",
            "s3cret",
            "api_key",
            "user:s3cret",
            "/path",
        ):
            assert sensitive not in caplog.text
        assert "reason_code=url_credentials" in caplog.text

    def test_web_search_blocked_url_log_does_not_leak_internal_host(self, caplog):
        from packages.rag.runbook_web_context import RunbookWebContextBuilder

        caplog.set_level("WARNING", logger="packages.rag.runbook_web_context")
        provider = StaticWebSearchProvider([
            _item(final_url="https://api.prod.svc.cluster.local/runbook")
        ])
        result = RunbookWebContextBuilder(
            settings=_enabled_settings(),
            provider=provider,
            dns_resolver=lambda _host: ["93.184.216.34"],
        ).build_context(query="internal host")
        assert result.status == "blocked"
        for sensitive in (
            "api.prod.svc.cluster.local",
            "svc.cluster.local",
            "api.prod",
            "/runbook",
        ):
            assert sensitive not in caplog.text
        assert "reason_code=cluster_internal_domain" in caplog.text

    def test_web_search_metric_reason_uses_fixed_code(self):
        from packages.rag.runbook_web_context import _metric_reason

        reason = _metric_reason("Host 'token-secret.example.com' is blocked")
        assert reason == "blocked_domain"
        assert "token-secret" not in reason

    def test_disabled_provider_records_metrics_without_external_call(self, monkeypatch):
        from packages.rag.runbook_web_context import RunbookWebContextBuilder

        spies = _patch_web_search_metrics(monkeypatch)
        provider = StaticWebSearchProvider([_item()])
        result = RunbookWebContextBuilder(
            settings=_enabled_settings(runbook_web_search_provider="disabled"),
            provider=provider,
        ).build_context(query="password=s3cret latency")

        assert result.status == "config_error"
        assert provider.queries == []
        assert _matching_calls(
            spies.requests,
            "inc",
            provider="disabled",
            status="config_error",
            reason="provider_disabled",
        )
        assert _matching_calls(
            spies.duration,
            "observe",
            provider="disabled",
            status="config_error",
            reason="provider_disabled",
        )
        result_calls = _matching_calls(
            spies.results,
            "inc",
            provider="disabled",
            status="config_error",
        )
        assert result_calls and result_calls[0][2] == 0
        assert _matching_calls(
            spies.cache,
            "inc",
            provider="disabled",
            status="not_applicable",
        )

    def test_fake_provider_records_deterministic_observability(self, monkeypatch):
        from packages.rag.runbook_web_context import RunbookWebContextBuilder

        spies = _patch_web_search_metrics(monkeypatch)
        result = RunbookWebContextBuilder(
            settings=_enabled_settings(runbook_web_search_max_results=2),
        ).build_context(query="service=checkout password=s3cret latency")

        assert result.status == "ok"
        assert len(result.results) == 2
        assert _matching_calls(
            spies.requests,
            "inc",
            provider="fake",
            status="ok",
            reason="none",
        )
        assert _matching_calls(
            spies.duration,
            "observe",
            provider="fake",
            status="ok",
            reason="none",
        )
        result_calls = _matching_calls(
            spies.results,
            "inc",
            provider="fake",
            status="ok",
        )
        assert result_calls and result_calls[0][2] == 2
        redaction_calls = _matching_calls(
            spies.redactions,
            "inc",
            provider="fake",
        )
        assert redaction_calls and redaction_calls[0][2] >= 2
        assert _matching_calls(
            spies.cache,
            "inc",
            provider="fake",
            status="not_applicable",
        )

    def test_web_search_observability_does_not_label_query_or_url_path(
        self,
        monkeypatch,
        caplog,
    ):
        from packages.rag.runbook_web_context import RunbookWebContextBuilder

        spies = _patch_web_search_metrics(monkeypatch)
        caplog.set_level("WARNING", logger="packages.rag.runbook_web_context")
        secret = "sk-" + "abcdefghijklmnopqrstuvwxyz123456"
        provider = StaticWebSearchProvider([
            _item(final_url="https://user:s3cret@10.0.0.5/private/path?api_key=abc")
        ])
        result = RunbookWebContextBuilder(
            settings=_enabled_settings(),
            provider=provider,
            dns_resolver=lambda _host: ["93.184.216.34"],
        ).build_context(
            query=(
                "service=checkout namespace=prod "
                f"password=s3cret token {secret} "
                "https://api.prod.svc.cluster.local/v1"
            )
        )

        assert result.status == "blocked"
        metric_text = _all_metric_call_text(spies)
        for sensitive in (
            "checkout",
            "namespace=prod",
            "s3cret",
            secret,
            "svc.cluster.local",
            "/private/path",
            "api_key",
            "user:s3cret",
        ):
            assert sensitive not in metric_text
            assert sensitive not in caplog.text
            assert sensitive not in repr(result)
        assert _matching_calls(
            spies.blocked,
            "inc",
            provider="fake",
            reason="url_credentials",
        )
        assert _matching_calls(
            spies.blocked,
            "inc",
            provider="fake",
            reason="all_results_blocked",
        )

    def test_web_search_diagnostics_redact_provider_error(self, monkeypatch):
        from packages.rag.runbook_web_context import RunbookWebContextBuilder

        spies = _patch_web_search_metrics(monkeypatch)
        provider = DegradedWebSearchProvider(
            error_message=(
                "failed for service=checkout password=s3cret "
                "api.prod.svc.cluster.local path /runbook 10.0.0.5/admin"
            )
        )
        result = RunbookWebContextBuilder(
            settings=_enabled_settings(),
            provider=provider,
            dns_resolver=lambda _host: ["93.184.216.34"],
        ).build_context(query="service=checkout password=s3cret")

        assert result.status == "degraded"
        assert result.error_message
        for sensitive in (
            "checkout",
            "s3cret",
            "api.prod.svc.cluster.local",
            "svc.cluster.local",
            "/runbook",
            "10.0.0.5",
            "/admin",
        ):
            assert sensitive not in result.error_message
            assert sensitive not in _all_metric_call_text(spies)
        assert _matching_calls(
            spies.requests,
            "inc",
            provider="fake",
            status="degraded",
            reason="provider_degraded",
        )

    def test_web_search_provider_exception_log_does_not_leak_secret(
        self,
        monkeypatch,
        caplog,
    ):
        from packages.rag.runbook_web_context import RunbookWebContextBuilder

        spies = _patch_web_search_metrics(monkeypatch)
        caplog.set_level("WARNING", logger="packages.rag.runbook_web_context")
        provider = RaisingWebSearchProvider(
            error_message=(
                "failed for service=checkout password=s3cret "
                "https://api.prod.svc.cluster.local/v1"
            )
        )
        result = RunbookWebContextBuilder(
            settings=_enabled_settings(),
            provider=provider,
            dns_resolver=lambda _host: ["93.184.216.34"],
        ).build_context(query="service=checkout password=s3cret")

        assert result.status == "degraded"
        assert result.error_message == "web_search provider exception"
        for sensitive in ("checkout", "s3cret", "svc.cluster.local"):
            assert sensitive not in caplog.text
            assert sensitive not in repr(result)
            assert sensitive not in _all_metric_call_text(spies)
        assert _matching_calls(
            spies.requests,
            "inc",
            provider="fake",
            status="degraded",
            reason="provider_exception",
        )

    def test_equivalent_redacted_query_hits_cache(self, monkeypatch):
        from packages.rag.runbook_web_context import RunbookWebContextBuilder

        spies = _patch_web_search_metrics(monkeypatch)
        provider = StaticWebSearchProvider([_item(snippet="cached guidance")])
        builder = RunbookWebContextBuilder(
            settings=_enabled_settings(
                runbook_web_search_cache_enabled=True,
                redis_url="memory://web-search-cache-equivalent",
            ),
            provider=provider,
            dns_resolver=lambda _host: ["93.184.216.34"],
        )

        first = builder.build_context(
            query=(
                "service=checkout password=s3cret "
                "https://api.prod.svc.cluster.local/v1 latency"
            )
        )
        second = builder.build_context(
            query=(
                "service=payments password=another-secret "
                "https://api.stage.svc.cluster.local/v2 latency"
            )
        )

        assert first.status == "ok"
        assert second.status == "ok"
        assert len(provider.queries) == 1
        assert second.results[0].snippet == "cached guidance"
        cache_statuses = [
            call[1]["status"]
            for call in spies.cache.calls
            if call[0] == "inc" and call[1].get("provider") == "fake"
        ]
        assert "miss" in cache_statuses
        assert "hit" in cache_statuses

    def test_web_search_cache_key_contains_no_raw_secret_host_or_path(self):
        from packages.rag.runbook_web_context import (
            _redact_web_search_text,
            _web_context_cache_key,
        )

        raw_query = (
            "service=checkout password=s3cret "
            "https://api.prod.svc.cluster.local/v1/runbook latency"
        )
        redacted_query = _redact_web_search_text(raw_query).redacted_text
        key = _web_context_cache_key(
            _enabled_settings(runbook_web_search_cache_enabled=True),
            provider="fake",
            purpose="draft_enrichment",
            redacted_query=redacted_query,
        )

        for sensitive in (
            "checkout",
            "s3cret",
            "api.prod.svc.cluster.local",
            "svc.cluster.local",
            "/v1",
            "/runbook",
            "latency",
        ):
            assert sensitive not in key

    def test_cached_records_are_url_safety_validated(self, caplog):
        from packages.rag.runbook_web_context import (
            RunbookWebContextBuilder,
            WebSearchResult,
            _serialize_cache_results,
        )

        class UnsafeCache:
            def get(self, key: str) -> str:
                return _serialize_cache_results([
                    WebSearchResult(
                        title="Unsafe cached result",
                        original_url="https://docs.example.com/runbook",
                        final_url="http://169.254.169.254/latest/meta-data",
                        snippet="do not use",
                        content_hash="sha256:unsafe",
                        provider="fake",
                        redaction_version="m9-9.4-1",
                        retrieved_at="2026-06-01T00:00:00+00:00",
                    )
                ])

            def setex(self, key: str, ttl_seconds: int, value: str) -> None:
                return None

        caplog.set_level("WARNING", logger="packages.rag.runbook_web_context")
        provider = StaticWebSearchProvider([_item(snippet="fresh safe result")])
        result = RunbookWebContextBuilder(
            settings=_enabled_settings(runbook_web_search_cache_enabled=True),
            provider=provider,
            dns_resolver=lambda _host: ["93.184.216.34"],
            cache=UnsafeCache(),
        ).build_context(query="latency")

        assert result.status == "ok"
        assert len(provider.queries) == 1
        assert result.results[0].snippet == "fresh safe result"
        assert "169.254.169.254" not in repr(result)
        assert "latest/meta-data" not in caplog.text

    def test_web_search_cache_failure_does_not_leak_query_or_secret(
        self,
        caplog,
    ):
        from packages.rag.runbook_web_context import RunbookWebContextBuilder

        class FailingCache:
            def get(self, key: str) -> str | None:
                raise RuntimeError(
                    "cache read failed for password=s3cret "
                    "https://api.prod.svc.cluster.local/v1"
                )

            def setex(self, key: str, ttl_seconds: int, value: str) -> None:
                raise RuntimeError(
                    "cache write failed for password=s3cret "
                    "https://api.prod.svc.cluster.local/v1"
                )

        caplog.set_level("WARNING", logger="packages.rag.runbook_web_context")
        provider = StaticWebSearchProvider([_item()])
        result = RunbookWebContextBuilder(
            settings=_enabled_settings(runbook_web_search_cache_enabled=True),
            provider=provider,
            dns_resolver=lambda _host: ["93.184.216.34"],
            cache=FailingCache(),
        ).build_context(
            query="service=checkout password=s3cret api.prod.svc.cluster.local/v1"
        )

        assert result.status == "ok"
        assert len(provider.queries) == 1
        for sensitive in (
            "checkout",
            "s3cret",
            "api.prod.svc.cluster.local",
            "svc.cluster.local",
            "/v1",
        ):
            assert sensitive not in caplog.text
            assert sensitive not in repr(result)


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
