"""Tests for M2 PR 2.6: Backend Endpoint Detector."""
from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from packages.common.settings import get_settings
from packages.discovery.backend_endpoints import BackendEndpointDetector
from packages.discovery.k8s_discovery import (
    K8sDiscoveryResult,
    K8sEndpoint,
    K8sIngress,
)


@dataclass
class _MockK8sService:
    name: str
    namespace: str = "default"
    ports: list[dict] = field(default_factory=list)
    labels: dict[str, str] = field(default_factory=dict)
    annotations: dict[str, str] = field(default_factory=dict)


def _setup_env(monkeypatch, **kwargs):
    for key, value in kwargs.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class TestBackendEndpointDetector:
    def test_detect_prometheus_service(self):
        detector = BackendEndpointDetector()
        services = [_MockK8sService("prometheus-k8s", "monitoring",
                                    ports=[{"name": "web", "port": 9090}])]
        endpoints = detector.detect(services)
        prom = [e for e in endpoints if e.backend_type == "prometheus"]
        assert len(prom) >= 1
        assert prom[0].url == "http://prometheus-k8s.monitoring.svc.cluster.local:9090"
        assert prom[0].status == "requires_review"

    def test_detect_loki_service_endpoint(self):
        detector = BackendEndpointDetector()
        services = [_MockK8sService("loki", "monitoring",
                                    ports=[{"name": "http", "port": 3100}])]
        endpoints = detector.detect(services)
        loki = [e for e in endpoints if e.backend_type == "loki"]
        assert loki[0].url.endswith(":3100")

    def test_detect_jaeger_query_service_endpoint(self):
        detector = BackendEndpointDetector()
        services = [_MockK8sService("jaeger-query", "observability",
                                    ports=[{"name": "query", "port": 16686}])]
        endpoints = detector.detect(services)
        jaeger = [e for e in endpoints if e.backend_type == "jaeger"]
        assert jaeger[0].url.endswith(":16686")

    def test_detect_alertmanager_service(self):
        detector = BackendEndpointDetector()
        services = [_MockK8sService("alertmanager-main", "monitoring",
                                    ports=[{"name": "web", "port": 9093}])]
        endpoints = detector.detect(services)
        am = [e for e in endpoints if e.backend_type == "alertmanager"]
        assert len(am) >= 1

    def test_manual_env_url_wins(self):
        detector = BackendEndpointDetector()
        services = [_MockK8sService("prometheus", "monitoring")]
        endpoints = detector.detect(
            services,
            manual_urls={"prometheus": "https://custom-prom:9090"},
        )
        prom = [e for e in endpoints if e.backend_type == "prometheus"]
        # Manual URL means no discovered endpoint for this type.
        assert len(prom) == 0

    def test_active_override_wins_over_discovery(self):
        detector = BackendEndpointDetector()
        services = [_MockK8sService("loki", "monitoring")]
        endpoints = detector.detect(
            services,
            manual_urls={"loki": "https://override-loki:3100"},
        )
        assert [e for e in endpoints if e.backend_type == "loki"] == []

    def test_no_backend_service_missing(self):
        detector = BackendEndpointDetector()
        endpoints = detector.detect([])
        # All 4 backend types should be returned as missing/degraded.
        backend_types = {e.backend_type for e in endpoints}
        assert "prometheus" in backend_types
        assert "loki" in backend_types

    def test_prefers_service_dns(self):
        detector = BackendEndpointDetector()
        services = [_MockK8sService("prometheus-operated", "monitoring")]
        endpoints = detector.detect(services)
        prom = [e for e in endpoints if e.backend_type == "prometheus"]
        if prom:
            url = prom[0].url
            assert ".svc.cluster.local" in url or prom[0].status == "degraded"

    def test_endpoint_evidence_from_k8s_endpoint(self):
        detector = BackendEndpointDetector()
        result = K8sDiscoveryResult(
            services=[_MockK8sService("prometheus", "monitoring",
                                      ports=[{"name": "web", "port": 9090}])],
            endpoints=[K8sEndpoint(
                name="prometheus",
                namespace="monitoring",
                addresses=["10.3.0.10"],
                ports=[{"name": "web", "port": 9090}],
            )],
        )
        prom = [e for e in detector.detect(result) if e.backend_type == "prometheus"][0]
        assert prom.evidence["endpoint"]["address_count"] == 1
        assert prom.confidence == 0.95

    def test_detect_ingress_only_endpoint(self):
        detector = BackendEndpointDetector()
        result = K8sDiscoveryResult(
            ingresses=[K8sIngress(
                name="prometheus-ing",
                namespace="monitoring",
                hosts=["prometheus.example.com"],
                tls_hosts=["prometheus.example.com"],
                service_names=["prometheus-k8s"],
            )],
        )
        prom = [e for e in detector.detect(result) if e.backend_type == "prometheus"][0]
        assert prom.source == "k8s_ingress"
        assert prom.url == "https://prometheus.example.com"
        assert prom.status == "requires_review"

    def test_low_confidence_endpoint_detected_only(self):
        detector = BackendEndpointDetector()
        services = [_MockK8sService("prometheus-proxy", "monitoring",
                                    ports=[{"name": "proxy", "port": 8080}])]
        prom = [e for e in detector.detect(services) if e.backend_type == "prometheus"][0]
        assert prom.status == "detected_only"

    def test_k8s_rbac_forbidden_backend_missing_degraded(self):
        detector = BackendEndpointDetector()
        result = K8sDiscoveryResult(
            degraded=True,
            degraded_reason="RBAC forbidden",
        )
        endpoints = detector.detect(result)
        assert all(e.status == "degraded" for e in endpoints)
        assert all(e.degraded_reason == "RBAC forbidden" for e in endpoints)

    def test_production_no_localhost_fallback_for_missing_backend(self, monkeypatch):
        _setup_env(monkeypatch, APP_ENV="production")
        detector = BackendEndpointDetector()
        endpoints = detector.detect([])
        assert all(e.url == "" for e in endpoints)
        assert all(e.status == "unavailable" for e in endpoints)

    def test_backend_url_discovery_production_requires_review(self, monkeypatch):
        _setup_env(monkeypatch, APP_ENV="production")
        detector = BackendEndpointDetector()
        services = [_MockK8sService("prometheus", "monitoring",
                                    ports=[{"name": "web", "port": 9090}])]
        prom = [e for e in detector.detect(services) if e.backend_type == "prometheus"][0]
        assert prom.status == "requires_review"

    def test_backend_url_auth_unknown_requires_review(self):
        detector = BackendEndpointDetector()
        services = [_MockK8sService("loki", "monitoring",
                                    ports=[{"name": "http", "port": 3100}])]
        loki = [e for e in detector.detect(services) if e.backend_type == "loki"][0]
        assert loki.auth_required_unknown is True
        assert loki.status == "requires_review"

    def test_multiple_backend_urls_require_review(self):
        detector = BackendEndpointDetector()
        services = [
            _MockK8sService("prometheus-a", "monitoring",
                            ports=[{"name": "web", "port": 9090}]),
            _MockK8sService("prometheus-b", "monitoring",
                            ports=[{"name": "web", "port": 9090}]),
        ]
        prom = [e for e in detector.detect(services) if e.backend_type == "prometheus"][0]
        assert prom.status == "requires_review"
        assert len(prom.evidence["candidates"]) == 2

    def test_detected_only_backend_not_published(self):
        detector = BackendEndpointDetector()
        services = [_MockK8sService("loki-proxy", "monitoring",
                                    ports=[{"name": "proxy", "port": 8080}])]
        loki = [e for e in detector.detect(services) if e.backend_type == "loki"][0]
        assert loki.status == "detected_only"

    def test_legacy_constructor_service_list_is_supported(self):
        detector = BackendEndpointDetector([
            _MockK8sService("prometheus", "monitoring",
                            ports=[{"name": "web", "port": 9090}])
        ])
        prom = [e for e in detector.detect() if e.backend_type == "prometheus"][0]
        assert prom.url.endswith(":9090")

    def test_legacy_constructor_discovery_result_is_supported(self):
        result = K8sDiscoveryResult(
            services=[
                _MockK8sService("loki", "monitoring",
                                ports=[{"name": "http", "port": 3100}])
            ]
        )
        detector = BackendEndpointDetector(result)
        loki = [e for e in detector.detect() if e.backend_type == "loki"][0]
        assert loki.url.endswith(":3100")

    def test_namespace_allowlist_filters_discovered_services(self):
        detector = BackendEndpointDetector(namespace_allowlist=["monitoring"])
        services = [
            _MockK8sService("prometheus", "dev",
                            ports=[{"name": "web", "port": 9090}]),
            _MockK8sService("loki", "monitoring",
                            ports=[{"name": "http", "port": 3100}]),
        ]
        endpoints = detector.detect(services)
        prom = [e for e in endpoints if e.backend_type == "prometheus"][0]
        loki = [e for e in endpoints if e.backend_type == "loki"][0]
        assert prom.url == ""
        assert prom.status == "degraded"
        assert loki.url.endswith(":3100")
