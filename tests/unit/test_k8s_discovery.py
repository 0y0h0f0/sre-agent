"""Tests for M2 PR 2.1: K8sDiscovery."""
from __future__ import annotations

import pytest

from packages.discovery.k8s_discovery import K8sDiscovery, K8sUnavailableError


class TestK8sDiscovery:
    def test_lazy_load_raises_when_kubernetes_missing(self):
        """K8sDiscovery raises K8sUnavailableError when kubernetes not installed."""
        discovery = K8sDiscovery()
        with pytest.raises(K8sUnavailableError):
            _ = discovery._k8s

    def test_discover_all_returns_degraded_when_k8s_unavailable(self):
        discovery = K8sDiscovery()
        result = discovery.discover_all()
        assert result.degraded is True
        assert result.degraded_reason is not None

    def test_namespace_allowlist_filtering(self):
        discovery = K8sDiscovery(namespace_allowlist=["prod", "staging"])
        assert "prod" in discovery._namespace_allowlist
        assert "staging" in discovery._namespace_allowlist

    def test_service_allowlist_defaults_empty(self):
        discovery = K8sDiscovery()
        assert discovery._service_allowlist == []

    def test_import_failure_is_cached(self):
        discovery1 = K8sDiscovery()
        discovery2 = K8sDiscovery()
        with pytest.raises(K8sUnavailableError):
            _ = discovery1._k8s
        # Second access should also fail (cached).
        with pytest.raises(K8sUnavailableError):
            _ = discovery2._k8s

    def test_thread_safety_initialization(self):
        """Multiple threads accessing _k8s should be safe."""
        import threading
        discovery = K8sDiscovery()
        errors = []

        def try_access():
            try:
                _ = discovery._k8s
            except K8sUnavailableError:
                errors.append("expected")

        threads = [threading.Thread(target=try_access) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # All should have raised K8sUnavailableError (kubernetes not installed).
        assert len(errors) == 3
