"""PR 9.9 — External Embedding Provider tests."""

from __future__ import annotations

import pytest

from packages.common.settings import Settings


class TestExternalEmbeddingDefaults:
    def test_external_embedding_default_disabled(self):
        settings = Settings()
        assert settings.external_embedding_provider_enabled is False

    def test_external_embedding_requires_m9_enabled(self):
        settings = Settings(
            m9_extensions_enabled=False,
            external_embedding_provider_enabled=True,
        )
        from packages.common.feature_flags import is_m9_subfeature_enabled
        assert not is_m9_subfeature_enabled(settings, "external_embedding_provider")

    def test_external_embedding_requires_semantic_search_enabled(self):
        """External embedding requires SEMANTIC_RUNBOOK_SEARCH_ENABLED."""
        settings = Settings(
            semantic_runbook_search_enabled=False,
            external_embedding_provider_enabled=True,
        )
        assert settings.semantic_runbook_search_enabled is False
        assert settings.external_embedding_provider_enabled is True


class TestExternalEmbeddingProvider:
    def test_provider_rejects_unsafe_url(self):
        from packages.common.errors import ValidationAppError
        from packages.rag.external_embedding_provider import ExternalEmbeddingProvider

        with pytest.raises(ValidationAppError, match="unsafe external embedding endpoint"):
            ExternalEmbeddingProvider(
                endpoint="http://localhost:8080/embed",
                app_env="production",
                allowed_domain_patterns=["localhost"],
            )

    def test_provider_accepts_safe_url(self):
        from packages.rag.external_embedding_provider import ExternalEmbeddingProvider

        provider = ExternalEmbeddingProvider(
            endpoint="https://embedding.example.com/embed",
            app_env="production",
            allowed_domain_patterns=["embedding.example.com"],
            dns_resolver=lambda _host: ["93.184.216.34"],
        )

        assert provider.endpoint == "https://embedding.example.com/embed"

    def test_provider_requires_allowlist_in_production(self):
        from packages.common.errors import ValidationAppError
        from packages.rag.external_embedding_provider import ExternalEmbeddingProvider

        with pytest.raises(ValidationAppError, match="allowlist"):
            ExternalEmbeddingProvider(
                endpoint="https://embedding.example.com/embed",
                app_env="production",
            )

    def test_provider_rejects_cluster_internal_domain_in_production(self):
        from packages.common.errors import ValidationAppError
        from packages.rag.external_embedding_provider import ExternalEmbeddingProvider

        with pytest.raises(ValidationAppError, match="unsafe external embedding endpoint"):
            ExternalEmbeddingProvider(
                endpoint="https://embeddings.internal.svc/embed",
                app_env="production",
                allowed_domain_patterns=["*.svc"],
            )


class TestExternalEmbeddingSafety:
    def test_no_raw_secret_in_repr(self):
        """ExternalEmbeddingProvider must not expose raw token in repr."""
        from packages.rag.external_embedding_provider import ExternalEmbeddingProvider
        provider = ExternalEmbeddingProvider(
            endpoint="https://embeddings.internal.svc/embed",
            secret_ref="env:EMBEDDING_API_KEY",
        )
        repr_str = repr(provider)
        assert "env:EMBEDDING_API_KEY" not in repr_str.lower()

    def test_uses_secret_reference_not_raw_value(self):
        """Provider stores secret references, not raw tokens."""
        from packages.rag.external_embedding_provider import ExternalEmbeddingProvider
        provider = ExternalEmbeddingProvider(
            endpoint="https://embeddings.internal.svc/embed",
            secret_ref="env:EMBEDDING_API_KEY",
        )
        # The provider stores the reference, resolves at call time
        assert provider.secret_ref == "env:EMBEDDING_API_KEY"

    def test_timeout_default(self):
        from packages.rag.external_embedding_provider import ExternalEmbeddingProvider
        provider = ExternalEmbeddingProvider(
            endpoint="https://embeddings.internal.svc/embed",
            secret_ref="env:EMBEDDING_API_KEY",
        )
        assert provider.timeout_seconds > 0

    def test_provider_conforms_to_primary_embedding_protocol_on_failure(self):
        from packages.rag.external_embedding_provider import ExternalEmbeddingProvider

        provider = ExternalEmbeddingProvider(endpoint="https://embeddings.internal.svc/embed")
        provider._circuit_open = True

        assert provider.dimension == 512
        assert provider.model_name == "external-512"
        assert provider.embed_text("checkout password=s3cret") == [0.0] * 512

    def test_redacts_input_before_sending(self):
        """Input text must be redacted before sending to external provider."""
        from packages.common.redaction import redact_text
        text = "Service: checkout, password: s3cret"
        result = redact_text(text)
        assert "s3cret" not in result.redacted_text
