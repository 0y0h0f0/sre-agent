"""Tests for PR 0.4: EffectiveConfig read priority."""

from __future__ import annotations

from packages.common.settings import Settings
from packages.discovery.config_merge import EffectiveConfig


def _local_settings() -> Settings:
    return Settings(_env_file=None)


def _production_settings() -> Settings:
    return Settings(_env_file=None, app_env="production")


class TestDemoPath:
    def test_demo_path_uses_settings_defaults(self):
        """from_demo_sources uses localhost defaults from settings."""
        settings = _local_settings()
        config = EffectiveConfig.from_demo_sources(settings)
        assert config.prometheus.url == "http://localhost:9090"
        assert config.loki.url == "http://localhost:3100"
        assert config.tempo.url == "http://localhost:3200"
        assert config.source == "demo"

    def test_demo_path_backward_compatible(self):
        """Demo path preserves service label settings."""
        settings = _local_settings()
        config = EffectiveConfig.from_demo_sources(settings)
        assert config.metrics_service_label == "service"
        assert config.logs_service_label == "service"


class TestEnvPriority:
    def test_env_has_highest_priority(self):
        """env settings override profile and published config."""
        settings = Settings(
            _env_file=None,
            prometheus_url="http://custom-prom:9090",
        )
        config = EffectiveConfig.from_operator_sources(
            settings,
            profile_overrides={"prometheus": "http://profile-prom:9090"},
            published_config={"prometheus_url": "http://pub:9090"},
        )
        assert config.prometheus.url == "http://custom-prom:9090"
        assert config.prometheus.source == "env"

    def test_override_beats_published_config(self):
        """Active override beats published discovery config."""
        settings = _local_settings()
        config = EffectiveConfig.from_operator_sources(
            settings,
            active_overrides=[
                {
                    "backend_type": "prometheus",
                    "url": "http://override-prom:9090",
                }
            ],
            published_config={"prometheus_url": "http://pub:9090"},
        )
        assert config.prometheus.url == "http://override-prom:9090"
        assert config.prometheus.source == "override"

    def test_profile_beats_published_config(self):
        """Profile overrides beat published config."""
        settings = _local_settings()
        config = EffectiveConfig.from_operator_sources(
            settings,
            profile_overrides={"prometheus": "http://profile-prom:9090"},
            published_config={"prometheus_url": "http://pub:9090"},
        )
        assert config.prometheus.url == "http://profile-prom:9090"
        assert config.prometheus.source == "profile"

    def test_published_config_used_when_no_env_or_override(self):
        """Published config used when nothing else is set."""
        settings = _local_settings()
        config = EffectiveConfig.from_operator_sources(
            settings,
            published_config={"prometheus_url": "http://pub:9090"},
        )
        assert config.prometheus.url == "http://pub:9090"
        assert config.prometheus.source == "published"

    def test_nested_published_config_used_when_no_env_or_override(self):
        """Published config may use either flat or nested backend URL shape."""
        settings = _local_settings()
        config = EffectiveConfig.from_operator_sources(
            settings,
            published_config={"prometheus": {"url": "http://pub:9090"}},
        )
        assert config.prometheus.url == "http://pub:9090"
        assert config.prometheus.source == "published"

    def test_tempo_published_config_is_available_for_trace_backend(self):
        """Tempo URL is resolved but not treated as a required base backend."""
        settings = _local_settings()
        config = EffectiveConfig.from_operator_sources(
            settings,
            published_config={"tempo_url": "http://tempo:3200"},
        )
        assert config.tempo.url == "http://tempo:3200"
        assert config.tempo.source == "published"


class TestProductionSafety:
    def test_production_rejects_implicit_localhost(self):
        """Production does NOT fall back to localhost defaults."""
        settings = _production_settings()
        config = EffectiveConfig.from_operator_sources(settings)
        assert config.prometheus.url is None
        assert config.prometheus.degraded is True

    def test_local_can_use_localhost_defaults(self):
        """Local env CAN use localhost defaults."""
        settings = _local_settings()
        config = EffectiveConfig.from_operator_sources(settings)
        assert config.prometheus.url == "http://localhost:9090"
        assert config.prometheus.degraded is False


class TestUnresolvedSources:
    def test_has_unresolved_returns_true_when_missing(self):
        """has_unresolved_required_sources returns True when a URL is missing."""
        settings = _production_settings()
        config = EffectiveConfig.from_operator_sources(settings)
        assert config.has_unresolved_required_sources() is True

    def test_has_unresolved_returns_false_when_all_set(self):
        """Returns False when all backends have URLs."""
        settings = _local_settings()
        config = EffectiveConfig.from_demo_sources(settings)
        assert config.has_unresolved_required_sources() is False

    def test_unresolved_sources_lists_missing(self):
        """unresolved_sources() lists backends with missing URLs."""
        settings = _production_settings()
        config = EffectiveConfig.from_operator_sources(settings)
        missing = config.unresolved_sources()
        assert "prometheus" in missing
        assert "loki" in missing


class TestWarnings:
    def test_degraded_backends_produce_warnings(self):
        """Production degraded backends produce warning messages."""
        settings = _production_settings()
        config = EffectiveConfig.from_operator_sources(settings)
        assert len(config.warnings) >= 1
        assert any("prometheus" in w for w in config.warnings)
