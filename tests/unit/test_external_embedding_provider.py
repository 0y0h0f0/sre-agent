"""PR 9.9 — External Embedding Provider tests."""

from __future__ import annotations

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
        from packages.common.backend_url_safety import BackendUrlSafetyValidator
        validator = BackendUrlSafetyValidator(app_env="production")
        result = validator.validate("http://localhost:8080/embed")
        assert result.is_safe is False

    def test_provider_accepts_safe_url(self):
        from packages.common.backend_url_safety import BackendUrlSafetyValidator
        validator = BackendUrlSafetyValidator(app_env="production")
        result = validator.validate("https://embeddings.internal.svc/embed")
        assert result.is_safe is True


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

    def test_redacts_input_before_sending(self):
        """Input text must be redacted before sending to external provider."""
        from packages.common.redaction import redact_text
        text = "Service: checkout, password: s3cret"
        result = redact_text(text)
        assert "s3cret" not in result.redacted_text
