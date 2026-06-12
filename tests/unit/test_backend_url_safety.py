"""Tests for PR 0.8: Backend URL Safety Validator."""

from __future__ import annotations

from packages.common.backend_url_safety import BackendUrlSafetyValidator


class TestProductionSafety:
    def test_rejects_metadata_ip(self):
        v = BackendUrlSafetyValidator(app_env="production")
        result = v.validate("http://169.254.169.254/latest/meta-data")
        assert result.is_safe is False

    def test_rejects_file_scheme(self):
        v = BackendUrlSafetyValidator(app_env="production")
        result = v.validate("file:///etc/passwd")
        assert result.is_safe is False

    def test_rejects_localhost_in_production(self):
        v = BackendUrlSafetyValidator(app_env="production")
        result = v.validate("http://localhost:9090")
        assert result.is_safe is False

    def test_rejects_link_local(self):
        v = BackendUrlSafetyValidator(app_env="production")
        result = v.validate("http://169.254.1.1:9090")
        assert result.is_safe is False

    def test_allows_https_public_endpoint(self):
        v = BackendUrlSafetyValidator(app_env="production")
        result = v.validate("https://prometheus.example.com:9090")
        assert result.is_safe is True

    def test_allows_allowlisted_internal_dns(self):
        v = BackendUrlSafetyValidator(
            app_env="production",
            allowlist_patterns=["*.svc.cluster.local", "*.monitoring.svc"],
        )
        result = v.validate("http://prometheus.monitoring.svc:9090")
        assert result.is_safe is True

    def test_rejects_private_ip_without_allowlist(self):
        v = BackendUrlSafetyValidator(app_env="production")
        result = v.validate("http://10.0.0.1:9090")
        assert result.is_safe is False

    def test_allows_private_ip_with_allowlist(self):
        v = BackendUrlSafetyValidator(
            app_env="production",
            allowlist_patterns=["10.0.0.1"],
        )
        result = v.validate("http://10.0.0.1:9090")
        assert result.is_safe is True


class TestLocalDev:
    def test_allows_localhost_in_local(self):
        v = BackendUrlSafetyValidator(app_env="local")
        result = v.validate("http://localhost:9090")
        assert result.is_safe is True

    def test_allows_private_ip_in_local(self):
        v = BackendUrlSafetyValidator(app_env="local")
        result = v.validate("http://192.168.1.1:9090")
        assert result.is_safe is True


class TestEdgeCases:
    def test_none_url_rejected(self):
        v = BackendUrlSafetyValidator()
        result = v.validate(None)
        assert result.is_safe is False

    def test_empty_url_rejected(self):
        v = BackendUrlSafetyValidator()
        result = v.validate("")
        assert result.is_safe is False

    def test_k8s_evidence_allows_cluster_service(self):
        v = BackendUrlSafetyValidator(
            app_env="production",
            k8s_evidence={
                "services": [
                    {"dns_name": "prometheus.monitoring.svc.cluster.local"}
                ]
            },
        )
        result = v.validate(
            "http://prometheus.monitoring.svc.cluster.local:9090"
        )
        assert result.is_safe is True
