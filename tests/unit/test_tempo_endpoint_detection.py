"""PR 9.6 — Tempo endpoint detection tests."""

from __future__ import annotations

from dataclasses import dataclass

from packages.common.settings import get_settings
from packages.discovery.backend_endpoints import BackendEndpointDetector


@dataclass
class FakeService:
    name: str
    namespace: str = "default"


def _setup_env(monkeypatch, **kwargs):
    for k, v in kwargs.items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()


class TestTempoEndpointDetection:
    def test_detect_tempo_service_endpoint(self, monkeypatch):
        _setup_env(monkeypatch, APP_ENV="local", M9_EXTENSIONS_ENABLED="true",
                   TEMPO_DISCOVERY_ENABLED="true")
        detector = BackendEndpointDetector()
        services = [
            FakeService(name="prometheus", namespace="monitoring"),
            FakeService(name="tempo", namespace="monitoring"),
        ]
        endpoints = detector.detect(services)
        tempo_eps = [e for e in endpoints if e.backend_type == "tempo"]
        assert len(tempo_eps) > 0

    def test_detect_tempo_ingress(self, monkeypatch):
        _setup_env(monkeypatch, APP_ENV="local", M9_EXTENSIONS_ENABLED="true",
                   TEMPO_DISCOVERY_ENABLED="true")
        detector = BackendEndpointDetector()
        services = [FakeService(name="tempo-query", namespace="observability")]
        endpoints = detector.detect(services)
        tempo_eps = [e for e in endpoints if e.backend_type == "tempo"]
        assert len(tempo_eps) > 0
        assert tempo_eps[0].url != ""


class TestTempoDiscoveryDefaultDisabled:
    def test_tempo_discovery_default_disabled(self):
        from packages.common.settings import Settings
        s = Settings()
        assert s.tempo_discovery_enabled is False

    def test_tempo_not_detected_when_gate_disabled(self, monkeypatch):
        _setup_env(monkeypatch, APP_ENV="local", M9_EXTENSIONS_ENABLED="false",
                   TEMPO_DISCOVERY_ENABLED="false")
        detector = BackendEndpointDetector()
        services = [FakeService(name="tempo", namespace="monitoring")]
        endpoints = detector.detect(services)
        tempo_eps = [e for e in endpoints if e.backend_type == "tempo"]
        assert tempo_eps == []


class TestTempoStateMachine:
    def test_endpoint_unsafe_url_rejected(self):
        from packages.common.backend_url_safety import BackendUrlSafetyValidator
        validator = BackendUrlSafetyValidator(app_env="production")
        result = validator.validate("http://localhost:3200/tempo")
        assert result.is_safe is False

    def test_endpoint_production_requires_review(self, monkeypatch):
        _setup_env(monkeypatch, APP_ENV="production", M9_EXTENSIONS_ENABLED="true",
                   TEMPO_DISCOVERY_ENABLED="true")
        detector = BackendEndpointDetector()
        services = [FakeService(name="tempo", namespace="monitoring")]
        endpoints = detector.detect(services)
        tempo_eps = [e for e in endpoints if e.backend_type == "tempo"]
        for ep in tempo_eps:
            assert ep.status != "ready", (
                f"Production tempo must not be 'ready', got {ep.status}"
            )

    def test_endpoint_production_never_auto_publish(self, monkeypatch):
        _setup_env(monkeypatch, APP_ENV="production", M9_EXTENSIONS_ENABLED="true",
                   TEMPO_DISCOVERY_ENABLED="true")
        detector = BackendEndpointDetector()
        services = [FakeService(name="tempo", namespace="monitoring")]
        endpoints = detector.detect(services)
        for ep in endpoints:
            if ep.backend_type == "tempo":
                assert ep.status != "ready"

    def test_endpoint_has_evidence(self, monkeypatch):
        _setup_env(monkeypatch, APP_ENV="local", M9_EXTENSIONS_ENABLED="true",
                   TEMPO_DISCOVERY_ENABLED="true")
        detector = BackendEndpointDetector()
        services = [FakeService(name="tempo", namespace="monitoring")]
        endpoints = detector.detect(services)
        tempo_eps = [e for e in endpoints if e.backend_type == "tempo"]
        for ep in tempo_eps:
            if ep.url:
                assert "k8s_service" in ep.evidence or ep.evidence.get("service_dns")


class TestTempoDiscoveryRespectsManualConfig:
    def test_endpoint_does_not_override_env(self, monkeypatch):
        _setup_env(monkeypatch, APP_ENV="local", M9_EXTENSIONS_ENABLED="true",
                   TEMPO_DISCOVERY_ENABLED="true")
        detector = BackendEndpointDetector()
        services = [FakeService(name="tempo", namespace="monitoring")]
        manual = {"tempo": "https://tempo.custom.internal:3200"}
        endpoints = detector.detect(services, manual_urls=manual)
        tempo_eps = [e for e in endpoints if e.backend_type == "tempo"]
        assert tempo_eps == []
