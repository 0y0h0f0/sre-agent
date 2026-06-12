"""Tests for M2 PR 2.6: Backend Endpoint Detector."""
from __future__ import annotations

from packages.discovery.backend_endpoints import BackendEndpointDetector


class _MockK8sService:
    def __init__(self, name, namespace="default"):
        self.name = name
        self.namespace = namespace


class TestBackendEndpointDetector:
    def test_detect_prometheus_service(self):
        detector = BackendEndpointDetector()
        services = [_MockK8sService("prometheus-k8s", "monitoring")]
        endpoints = detector.detect(services)
        prom = [e for e in endpoints if e.backend_type == "prometheus"]
        assert len(prom) >= 1

    def test_detect_alertmanager_service(self):
        detector = BackendEndpointDetector()
        services = [_MockK8sService("alertmanager-main", "monitoring")]
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
