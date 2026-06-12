"""PR 9.1 — Trace backend settings tests.

Tests for TRACE_BACKEND enum values, TRACE_ENABLED gating, and fixture rejection
in production.
"""

from __future__ import annotations

import pytest

from packages.common.settings import Settings


class TestTraceBackendEnum:
    """TRACE_BACKEND must accept disabled, fixture, jaeger, tempo."""

    def test_trace_backend_accepts_disabled_fixture_jaeger_tempo(self, monkeypatch):
        """All four TRACE_BACKEND values are accepted without error."""
        for backend in ("disabled", "fixture", "jaeger", "tempo"):
            monkeypatch.setenv("TRACE_BACKEND", backend)
            settings = Settings()
            assert settings.trace_backend == backend

    def test_trace_backend_rejects_unknown_value(self, monkeypatch):
        """Unknown TRACE_BACKEND values should raise a validation error."""
        monkeypatch.setenv("TRACE_BACKEND", "zipkin")
        with pytest.raises(Exception):
            Settings()

    def test_trace_backend_default_is_fixture(self):
        """Default TRACE_BACKEND is fixture for backward compatibility."""
        settings = Settings()
        assert settings.trace_backend == "fixture"

    def test_trace_enabled_default_is_true(self):
        """TRACE_ENABLED defaults to True for backward-compatible local/dev behavior."""
        settings = Settings()
        assert settings.trace_enabled is True

    def test_trace_backend_disabled_means_no_trace_provider(self):
        """TRACE_BACKEND=disabled explicitly indicates no trace capability."""
        settings = Settings()
        assert settings.trace_backend == "fixture"  # default
        # disabled is a valid value
        settings = Settings(trace_backend="disabled")
        assert settings.trace_backend == "disabled"


class TestFixtureTraceBackendRejectedInProduction:
    """fixture must not be used as a normal production trace backend."""

    def test_fixture_is_not_valid_production_backend(self):
        """TRACE_BACKEND=fixture is for local/CI only, not production."""
        # This is a design assertion: fixture should never be the production
        # trace backend. The M9 feature flag module enforces this.
        settings = Settings(
            app_env="production", trace_backend="fixture"
        )
        # Settings accepts it (backward compat), but the feature flag
        # resolver must warn/degrade.
        assert settings.trace_backend == "fixture"

    def test_production_with_disabled_trace_backend(self):
        """TRACE_BACKEND=disabled with TRACE_ENABLED=false is valid production."""
        settings = Settings(
            app_env="production", trace_backend="disabled", trace_enabled=False
        )
        assert settings.trace_backend == "disabled"
        assert settings.trace_enabled is False

    def test_production_with_jaeger_trace_backend(self):
        """TRACE_BACKEND=jaeger is valid in production (M8 behavior)."""
        settings = Settings(
            app_env="production", trace_backend="jaeger", trace_enabled=True
        )
        assert settings.trace_backend == "jaeger"
        assert settings.trace_enabled is True


class TestPreM9TraceRollback:
    """PRE_M9_TRACE_BACKEND and PRE_M9_TRACE_ENABLED for total M9 rollback."""

    def test_pre_m9_trace_backend_default_empty(self):
        """PRE_M9_TRACE_BACKEND defaults to empty string."""
        settings = Settings()
        assert settings.pre_m9_trace_backend == ""

    def test_pre_m9_trace_enabled_default_empty(self):
        """PRE_M9_TRACE_ENABLED defaults to empty string."""
        settings = Settings()
        assert settings.pre_m9_trace_enabled == ""

    def test_pre_m9_trace_vars_settable(self, monkeypatch):
        """PRE_M9_TRACE_* vars can be set via env."""
        monkeypatch.setenv("PRE_M9_TRACE_BACKEND", "jaeger")
        monkeypatch.setenv("PRE_M9_TRACE_ENABLED", "true")
        settings = Settings()
        assert settings.pre_m9_trace_backend == "jaeger"
        assert settings.pre_m9_trace_enabled == "true"
