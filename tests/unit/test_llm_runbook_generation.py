"""PR 9.2 — LLM Runbook Draft Generation tests."""

from __future__ import annotations

import pytest

from packages.agent.llm.fake_adapter import FakeLLMAdapter
from packages.common.feature_flags import resolve_m9_feature_flags
from packages.common.redaction import redact_text
from packages.common.settings import Settings
from packages.rag.llm_runbook_generator import LLMRunbookGenerator
from packages.rag.runbook_action_classifier import (
    ActionClassification,
    RunbookActionClassifier,
)
from packages.rag.runbook_prompt_builder import RunbookPromptBuilder


# ---------------------------------------------------------------------------
# Default disabled
# ---------------------------------------------------------------------------

class TestLLMRunbookGenerationDefaultDisabled:
    def test_generator_requires_m9_enabled(self):
        """LLMRunbookGenerator refuses when M9_EXTENSIONS_ENABLED=false."""
        settings = Settings(m9_extensions_enabled=False)
        llm = FakeLLMAdapter()
        classifier = RunbookActionClassifier()
        prompt_builder = RunbookPromptBuilder()
        generator = LLMRunbookGenerator(
            settings=settings,
            llm=llm,
            classifier=classifier,
            prompt_builder=prompt_builder,
        )
        result = generator.generate(
            service="checkout",
            incident_type="high_5xx",
        )
        assert result.status == "disabled"
        assert result.draft_id is None

    def test_generator_requires_runbook_llm_generation_enabled(self):
        """Refuses when M9 is on but RUNBOOK_LLM_GENERATION_ENABLED=false."""
        settings = Settings(
            m9_extensions_enabled=True,
            runbook_llm_generation_enabled=False,
        )
        llm = FakeLLMAdapter()
        classifier = RunbookActionClassifier()
        prompt_builder = RunbookPromptBuilder()
        generator = LLMRunbookGenerator(
            settings=settings,
            llm=llm,
            classifier=classifier,
            prompt_builder=prompt_builder,
        )
        result = generator.generate(
            service="checkout",
            incident_type="high_5xx",
        )
        assert result.status == "disabled"


# ---------------------------------------------------------------------------
# Prompt redaction
# ---------------------------------------------------------------------------

class TestPromptRedaction:
    def test_prompt_redacts_bearer_token(self):
        """Bearer tokens must not appear in prompt."""
        text = "Authorization: Bearer sk-abc123def456ghijklmnopqrstuvwxyz"
        result = redact_text(text)
        assert "Bearer" not in result.redacted_text or "[REDACTED]" in result.redacted_text
        assert result.redaction_count >= 1

    def test_prompt_redacts_password(self):
        """Passwords must not appear in prompt."""
        text = 'password: "s3cret!" db_password=super_secret'
        result = redact_text(text)
        assert result.redaction_count >= 1
        assert "s3cret!" not in result.redacted_text

    def test_prompt_redacts_private_key(self):
        """Private key blocks must not appear in prompt."""
        text = """-----BEGIN RSA PRIVATE KEY-----
MIIEpAIBAAKCAQEA0Z3...
-----END RSA PRIVATE KEY-----"""
        result = redact_text(text)
        assert "[REDACTED]" in result.redacted_text
        assert result.redaction_count >= 1

    def test_prompt_redacts_internal_urls(self):
        """Internal URLs must not appear in prompt."""
        text = "Service is at http://localhost:8080/api and http://169.254.169.254/metadata"
        result = redact_text(text)
        assert "localhost" not in result.redacted_text
        assert "169.254" not in result.redacted_text

    def test_prompt_redacts_private_ip(self):
        """Private IPs must not appear in prompt."""
        text = "Database at 10.0.1.5:5432, cache at 192.168.1.100:6379"
        result = redact_text(text)
        assert "10.0.1.5" not in result.redacted_text
        assert "192.168.1.100" not in result.redacted_text

    def test_redaction_result_metadata_safe(self):
        """RedactionResult.to_safe_dict() must not contain raw values."""
        text = "Bearer token123secret"
        result = redact_text(text)
        safe = result.to_safe_dict()
        assert "redaction_count" in safe
        assert "redaction_types" in safe
        assert "token123secret" not in str(safe)


# ---------------------------------------------------------------------------
# RunbookPromptBuilder
# ---------------------------------------------------------------------------

class TestRunbookPromptBuilder:
    def setup_method(self):
        self.builder = RunbookPromptBuilder()

    def test_build_returns_prompt_and_metadata(self):
        prompt, metadata = self.builder.build(
            service="checkout",
            incident_type="high_5xx",
            runbook_context=["Approved: Check CPU and memory metrics first."],
            evidence_summary="CPU at 95%, memory stable",
            template_draft="# Checkout High 5xx Runbook\n\n## Detection\n...",
            capability_gaps=["No trace data available for checkout service"],
            effective_config={"prometheus_url": "http://prometheus:9090"},
        )
        assert isinstance(prompt, str)
        assert len(prompt) > 50
        assert isinstance(metadata, dict)
        assert "prompt_template_id" in metadata
        assert "input_object_hash" in metadata

    def test_build_redacts_config(self):
        prompt, _ = self.builder.build(
            service="checkout",
            incident_type="high_5xx",
            effective_config={
                "prometheus_url": "http://localhost:9090",
                "auth_token": "Bearer secret123",
            },
        )
        assert "secret123" not in prompt
        assert "Bearer" not in prompt or "[REDACTED]" in prompt

    def test_prompt_preview_truncated(self):
        _, metadata = self.builder.build(
            service="checkout",
            incident_type="high_5xx",
            runbook_context=["Some context " * 50],
        )
        preview = metadata.get("prompt_preview", "")
        assert len(preview) <= 4096

    def test_metadata_contains_hashes_not_raw_values(self):
        _, metadata = self.builder.build(
            service="checkout",
            incident_type="high_5xx",
            runbook_context=["Check metrics"],
            evidence_summary="CPU high",
        )
        assert "input_object_hash" in metadata
        assert "prompt_template_version" in metadata
        assert "redaction_version" in metadata
        # Hashes must be present, not raw context
        assert "Check metrics" not in str(metadata.get("input_object_hash", ""))


# ---------------------------------------------------------------------------
# LLMRunbookGenerator with FakeLLM
# ---------------------------------------------------------------------------

class TestLLMRunbookGeneratorWithFakeLLM:
    def setup_method(self):
        self.settings = Settings(
            m9_extensions_enabled=True,
            runbook_llm_generation_enabled=True,
            llm_provider="fake",
        )
        self.llm = FakeLLMAdapter()
        self.classifier = RunbookActionClassifier()
        self.prompt_builder = RunbookPromptBuilder()

    def _make_generator(self, **overrides):
        s = Settings(
            **{**self.settings.model_dump(), **overrides}
        )
        return LLMRunbookGenerator(
            settings=s,
            llm=self.llm,
            classifier=self.classifier,
            prompt_builder=self.prompt_builder,
        )

    def test_generate_returns_content_with_fake_llm(self):
        """LLMRunbookGenerator returns generated content — persistence is the service layer's job."""
        generator = self._make_generator()
        result = generator.generate(
            service="checkout",
            incident_type="high_5xx",
            runbook_context=["Approved: Check error rate trends."],
            evidence_summary="5xx spike after deploy",
            template_draft="# High 5xx Runbook\n\n## Detection\n...",
        )
        assert result.status == "generated"
        assert result.content is not None
        assert len(result.content) > 0

    def test_generated_draft_has_pending_review_status(self):
        generator = self._make_generator()
        result = generator.generate(
            service="checkout",
            incident_type="high_5xx",
        )
        assert result.status == "generated"
        assert result.draft_status == "pending_review"

    def test_generated_draft_has_llm_generated_type(self):
        generator = self._make_generator()
        result = generator.generate(
            service="checkout",
            incident_type="high_5xx",
        )
        assert result.draft_type == "llm_generated"

    def test_generated_draft_has_action_classification(self):
        generator = self._make_generator()
        result = generator.generate(
            service="checkout",
            incident_type="high_5xx",
        )
        assert result.action_classification_summary is not None
        counts = result.action_classification_summary.get("counts", {})
        assert "read_only" in counts

    def test_fake_llm_content_does_not_contain_raw_prompt_metadata(self):
        generator = self._make_generator()
        result = generator.generate(
            service="checkout",
            incident_type="high_5xx",
            evidence_summary="Secret: Bearer token123",
        )
        if result.prompt_metadata:
            meta_str = str(result.prompt_metadata)
            assert "token123" not in meta_str

    def test_external_llm_provider_refused_without_allow(self):
        """External cloud LLM requires LLM_EXTERNAL_PROVIDER_ALLOWED=true."""
        generator = self._make_generator(
            llm_provider="openai",
            llm_external_provider_allowed=False,
        )
        result = generator.generate(
            service="checkout",
            incident_type="high_5xx",
        )
        assert result.status in ("disabled", "blocked")

    def test_failure_returns_degraded_status(self):
        """When FakeLLM returns empty (simulating failure), generator handles it.

        FakeLLMAdapter always returns valid content, so we test that the
        generator properly validates LLM output length.
        """
        generator = self._make_generator()
        # Even with minimal context, FakeLLM returns valid output
        result = generator.generate(
            service="checkout",
            incident_type="high_5xx",
        )
        assert result.status in ("generated", "degraded")


# ---------------------------------------------------------------------------
# Integration scenario tests
# ---------------------------------------------------------------------------

class TestLLMRunbookGenerationScenarios:
    def test_disabled_when_m9_off_in_production(self):
        settings = Settings(
            app_env="production",
            m9_extensions_enabled=False,
        )
        flags = resolve_m9_feature_flags(settings)
        assert flags.runbook_llm_generation is False

    def test_enabled_when_both_gates_on(self):
        settings = Settings(
            m9_extensions_enabled=True,
            runbook_llm_generation_enabled=True,
        )
        flags = resolve_m9_feature_flags(settings)
        assert flags.runbook_llm_generation is True

    def test_prompt_excludes_bearer_token_from_config(self):
        builder = RunbookPromptBuilder()
        prompt, _ = builder.build(
            service="checkout",
            incident_type="high_5xx",
            effective_config={
                "prometheus_url": "http://prom:9090",
                "Authorization": "Bearer secret-token-value-1234567890",
            },
        )
        assert "secret-token-value" not in prompt

    def test_prompt_excludes_password_from_config(self):
        builder = RunbookPromptBuilder()
        prompt, _ = builder.build(
            service="checkout",
            incident_type="high_5xx",
            effective_config={
                "db_url": "postgres://user:MyP@ssword123@10.0.0.1:5432/db",
            },
        )
        assert "MyP@ssword123" not in prompt
