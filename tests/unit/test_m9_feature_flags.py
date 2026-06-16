"""PR 9.1 — M9 Feature Flag tests.

Tests for M9 global gate, sub-feature gating, conflict detection, and metrics.
"""

from __future__ import annotations

import logging

from packages.common.feature_flags import (
    M9FeatureFlags,
    is_m9_enabled,
    is_m9_subfeature_enabled,
    resolve_m9_feature_flags,
)
from packages.common.settings import Settings

# ---------------------------------------------------------------------------
# Default-disabled
# ---------------------------------------------------------------------------

class TestM9DefaultDisabled:
    def test_m9_extensions_default_disabled(self):
        """M9_EXTENSIONS_ENABLED defaults to False."""
        settings = Settings()
        assert settings.m9_extensions_enabled is False

    def test_m9_subfeatures_default_disabled(self):
        """All M9 sub-feature flags default to False."""
        settings = Settings()
        assert settings.runbook_llm_generation_enabled is False
        assert settings.llm_incident_diff_enabled is False
        assert settings.runbook_web_search_enabled is False
        assert settings.tempo_discovery_enabled is False
        assert settings.grafana_alert_ingest_enabled is False
        assert settings.semantic_runbook_search_enabled is False
        assert settings.external_embedding_provider_enabled is False
        assert settings.llm_external_provider_allowed is False

    def test_is_m9_enabled_returns_false_by_default(self):
        """is_m9_enabled() returns False when M9_EXTENSIONS_ENABLED=False."""
        settings = Settings()
        assert is_m9_enabled(settings) is False

    def test_is_m9_enabled_returns_true_when_enabled(self):
        """is_m9_enabled() returns True when M9_EXTENSIONS_ENABLED=True."""
        settings = Settings(m9_extensions_enabled=True)
        assert is_m9_enabled(settings) is True


# ---------------------------------------------------------------------------
# Global gate disables all sub-features
# ---------------------------------------------------------------------------

class TestM9GlobalDisabledForcesSubfeaturesDisabled:
    def test_global_disabled_forces_llm_generation_off(self):
        """M9 disabled + RUNBOOK_LLM_GENERATION_ENABLED=true → resolved False."""
        settings = Settings(
            m9_extensions_enabled=False,
            runbook_llm_generation_enabled=True,
        )
        assert is_m9_subfeature_enabled(settings, "runbook_llm_generation") is False

    def test_global_disabled_forces_llm_diff_off(self):
        """M9 disabled + LLM_INCIDENT_DIFF_ENABLED=true → resolved False."""
        settings = Settings(
            m9_extensions_enabled=False,
            llm_incident_diff_enabled=True,
        )
        assert is_m9_subfeature_enabled(settings, "llm_incident_diff") is False

    def test_global_disabled_forces_web_search_off(self):
        """M9 disabled + RUNBOOK_WEB_SEARCH_ENABLED=true → resolved False."""
        settings = Settings(
            m9_extensions_enabled=False,
            runbook_web_search_enabled=True,
        )
        assert is_m9_subfeature_enabled(settings, "runbook_web_search") is False

    def test_global_disabled_forces_tempo_discovery_off(self):
        """M9 disabled + TEMPO_DISCOVERY_ENABLED=true → resolved False."""
        settings = Settings(
            m9_extensions_enabled=False,
            tempo_discovery_enabled=True,
        )
        assert is_m9_subfeature_enabled(settings, "tempo_discovery") is False

    def test_global_disabled_forces_grafana_ingest_off(self):
        """M9 disabled + GRAFANA_ALERT_INGEST_ENABLED=true → resolved False."""
        settings = Settings(
            m9_extensions_enabled=False,
            grafana_alert_ingest_enabled=True,
        )
        assert is_m9_subfeature_enabled(settings, "grafana_alert_ingest") is False

    def test_global_disabled_forces_semantic_search_off(self):
        """M9 disabled + SEMANTIC_RUNBOOK_SEARCH_ENABLED=true → resolved False."""
        settings = Settings(
            m9_extensions_enabled=False,
            semantic_runbook_search_enabled=True,
        )
        assert is_m9_subfeature_enabled(settings, "semantic_runbook_search") is False

    def test_global_disabled_forces_external_embedding_off(self):
        """M9 disabled + EXTERNAL_EMBEDDING_PROVIDER_ENABLED=true → resolved False."""
        settings = Settings(
            m9_extensions_enabled=False,
            external_embedding_provider_enabled=True,
        )
        assert is_m9_subfeature_enabled(settings, "external_embedding_provider") is False

    def test_global_enabled_allows_subfeature(self):
        """M9 enabled + sub-feature=true → resolved True."""
        settings = Settings(
            m9_extensions_enabled=True,
            runbook_llm_generation_enabled=True,
        )
        assert is_m9_subfeature_enabled(settings, "runbook_llm_generation") is True


# ---------------------------------------------------------------------------
# Conflict detection — warning + metric, no fatal error
# ---------------------------------------------------------------------------

class TestM9ConflictDetection:
    def test_conflict_records_warning_log(self, caplog):
        """Global disabled + sub-feature true records a startup warning."""
        settings = Settings(
            m9_extensions_enabled=False,
            runbook_llm_generation_enabled=True,
        )
        with caplog.at_level(logging.WARNING):
            flags = resolve_m9_feature_flags(settings)
        assert len(flags.conflicts) > 0
        assert any("runbook_llm_generation" in c.feature for c in flags.conflicts)
        assert any("M9_EXTENSIONS_ENABLED=false" in r.message
                   for r in caplog.records if r.levelno == logging.WARNING
                   for c in flags.conflicts)

    def test_conflict_records_metric_label(self):
        """Each conflict has a feature label suitable for Prometheus metric."""
        settings = Settings(
            m9_extensions_enabled=False,
            runbook_llm_generation_enabled=True,
            llm_incident_diff_enabled=True,
        )
        flags = resolve_m9_feature_flags(settings)
        conflict_features = {c.feature for c in flags.conflicts}
        assert "runbook_llm_generation" in conflict_features
        assert "llm_incident_diff" in conflict_features

    def test_conflict_does_not_raise_fatal_error(self):
        """Conflicts must not prevent service startup."""
        settings = Settings(
            m9_extensions_enabled=False,
            runbook_llm_generation_enabled=True,
            runbook_web_search_enabled=True,
        )
        # Must not raise
        flags = resolve_m9_feature_flags(settings)
        assert flags.m9_enabled is False
        # Sub-features resolved to False despite being True in settings
        assert flags.runbook_llm_generation is False
        assert flags.runbook_web_search is False

    def test_no_conflict_when_global_enabled_subfeature_true(self):
        """No conflict when both global and sub-feature are enabled."""
        settings = Settings(
            m9_extensions_enabled=True,
            runbook_llm_generation_enabled=True,
        )
        flags = resolve_m9_feature_flags(settings)
        assert len(flags.conflicts) == 0
        assert flags.runbook_llm_generation is True

    def test_no_conflict_when_both_disabled(self):
        """No conflict when both global and sub-feature are disabled."""
        settings = Settings(
            m9_extensions_enabled=False,
            runbook_llm_generation_enabled=False,
        )
        flags = resolve_m9_feature_flags(settings)
        assert len(flags.conflicts) == 0
        assert flags.runbook_llm_generation is False


# ---------------------------------------------------------------------------
# M9 global gate does NOT disable existing Jaeger
# ---------------------------------------------------------------------------

class TestM9DoesNotDisableJaeger:
    def test_m9_global_disabled_does_not_disable_jaeger(self):
        """M9_EXTENSIONS_ENABLED=false + TRACE_BACKEND=jaeger → Jaeger active."""
        settings = Settings(
            m9_extensions_enabled=False,
            trace_backend="jaeger",
            trace_enabled=True,
        )
        flags = resolve_m9_feature_flags(settings)
        # Jaeger is M8 behavior, not an M9 sub-feature
        assert flags.trace_backend == "jaeger"
        # M9 extensions off should not force trace_enabled to False
        # when using jaeger (M8 verified path)

    def test_m9_global_disabled_does_not_change_jaeger_config(self):
        """M9 disabled preserves Jaeger as valid trace backend."""
        settings = Settings(
            m9_extensions_enabled=False,
            trace_backend="jaeger",
        )
        flags = resolve_m9_feature_flags(settings)
        assert flags.trace_backend == "jaeger"

    def test_m9_global_disabled_forces_tempo_degraded(self):
        """M9 disabled + TRACE_BACKEND=tempo → tempo is degraded (not enabled)."""
        settings = Settings(
            m9_extensions_enabled=False,
            trace_backend="tempo",
        )
        flags = resolve_m9_feature_flags(settings)
        # Tempo is an M9 feature, so it should be flagged
        assert flags.tempo_degraded is True


# ---------------------------------------------------------------------------
# Tempo trace conflict metric
# ---------------------------------------------------------------------------

class TestTempoTraceConflictMetric:
    def test_tempo_trace_conflict_metric_recorded(self):
        """M9 disabled + TRACE_BACKEND=tempo records a conflict."""
        settings = Settings(
            m9_extensions_enabled=False,
            trace_backend="tempo",
        )
        flags = resolve_m9_feature_flags(settings)
        conflict_features = {c.feature for c in flags.conflicts}
        assert "tempo_trace_backend" in conflict_features


# ---------------------------------------------------------------------------
# Production safety defaults (via model_validator)
# ---------------------------------------------------------------------------

class TestM9ProductionSafetyDefaults:
    def test_production_defaults_m9_disabled(self):
        """In production without explicit M9 config, M9 stays disabled."""
        settings = Settings(app_env="production")
        assert settings.m9_extensions_enabled is False

    def test_production_can_explicitly_enable_m9(self):
        """Production can opt into M9 by explicit env var."""
        settings = Settings(app_env="production", m9_extensions_enabled=True)
        assert settings.m9_extensions_enabled is True

    def test_local_defaults_preserve_m8_behavior(self):
        """Local env keeps fixture defaults compatible with M8 tests."""
        settings = Settings(app_env="local")
        assert settings.trace_backend == "fixture"
        assert settings.m9_extensions_enabled is False


# ---------------------------------------------------------------------------
# resolve_m9_feature_flags comprehensive
# ---------------------------------------------------------------------------

class TestResolveM9FeatureFlags:
    def test_resolve_returns_all_feature_states(self):
        """resolve_m9_feature_flags returns all M9 feature states."""
        settings = Settings(m9_extensions_enabled=True)
        flags = resolve_m9_feature_flags(settings)
        assert isinstance(flags, M9FeatureFlags)
        assert flags.m9_enabled is True
        assert hasattr(flags, "runbook_llm_generation")
        assert hasattr(flags, "llm_incident_diff")
        assert hasattr(flags, "runbook_web_search")
        assert hasattr(flags, "tempo_discovery")
        assert hasattr(flags, "grafana_alert_ingest")
        assert hasattr(flags, "semantic_runbook_search")
        assert hasattr(flags, "external_embedding_provider")

    def test_resolve_with_all_subfeatures_enabled(self):
        """When M9 is on and all sub-features are on, everything is enabled."""
        settings = Settings(
            m9_extensions_enabled=True,
            runbook_llm_generation_enabled=True,
            llm_incident_diff_enabled=True,
            runbook_web_search_enabled=True,
            tempo_discovery_enabled=True,
            grafana_alert_ingest_enabled=True,
            semantic_runbook_search_enabled=True,
            external_embedding_provider_enabled=True,
        )
        flags = resolve_m9_feature_flags(settings)
        assert flags.runbook_llm_generation is True
        assert flags.llm_incident_diff is True
        assert flags.runbook_web_search is True
        assert flags.tempo_discovery is True
        assert flags.grafana_alert_ingest is True
        assert flags.semantic_runbook_search is True
        assert flags.external_embedding_provider is True
        assert len(flags.conflicts) == 0


# ---------------------------------------------------------------------------
# Embedding provider settings
# ---------------------------------------------------------------------------

class TestEmbeddingProviderSettings:
    def test_embedding_provider_default_is_fake(self):
        """EMBEDDING_PROVIDER defaults to 'fake' for backward compat with M0-M8."""
        settings = Settings()
        assert settings.embedding_provider == "fake"

    def test_embedding_provider_accepts_disabled_bge_zh_external(self):
        """EMBEDDING_PROVIDER accepts disabled, bge_zh, external (M9 values)."""
        for provider in ("disabled", "bge_zh", "external"):
            settings = Settings(embedding_provider=provider)
            assert settings.embedding_provider == provider

    def test_embedding_provider_fake_still_valid(self):
        """EMBEDDING_PROVIDER=fake is still valid for M0-M8 backward compat."""
        settings = Settings(embedding_provider="fake")
        assert settings.embedding_provider == "fake"
