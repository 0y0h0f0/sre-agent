"""Tests for M2 PR 2.7: Jaeger Service Discovery."""
from __future__ import annotations

from unittest.mock import MagicMock

import httpx

from packages.discovery.jaeger_discovery import (
    JaegerDiscoveryClient,
)


def _resp(status=200, data=None):
    r = MagicMock(spec=httpx.Response)
    r.status_code = status
    r.json.return_value = data or {}
    r.text = ""
    return r


class TestJaegerDiscoveryClient:
    def test_list_services_success(self):
        mock = MagicMock(spec=httpx.Client)
        mock.get.return_value = _resp(200, {"data": ["checkout", "payments"]})
        c = JaegerDiscoveryClient("http://localhost:16686", client=mock)
        result = c.list_services()
        assert result.status == "available"
        assert "checkout" in result.available_services

    def test_discover_services_alias(self):
        mock = MagicMock(spec=httpx.Client)
        mock.get.return_value = _resp(200, {"data": ["checkout"]})
        c = JaegerDiscoveryClient("http://localhost:16686", client=mock)
        result = c.discover_services()
        assert result.status == "available"
        assert result.available_services == ["checkout"]

    def test_unavailable_degraded(self):
        mock = MagicMock(spec=httpx.Client)
        mock.get.side_effect = httpx.TimeoutException("timeout")
        c = JaegerDiscoveryClient("http://localhost:16686", client=mock)
        result = c.list_services()
        assert result.status == "degraded"
        assert result.available_services == []

    def test_auth_error_degraded(self):
        mock = MagicMock(spec=httpx.Client)
        mock.get.return_value = _resp(401)
        c = JaegerDiscoveryClient("http://localhost:16686", client=mock)
        result = c.list_services()
        assert result.status == "degraded"

    def test_no_raw_secret_in_output(self):
        c = JaegerDiscoveryClient("http://localhost:16686")
        redacted = c.redacted_auth
        assert not hasattr(redacted, "token")
        assert redacted.auth_type == "none"

    def test_empty_services_degraded(self):
        mock = MagicMock(spec=httpx.Client)
        mock.get.return_value = _resp(200, {"data": []})
        c = JaegerDiscoveryClient("http://localhost:16686", client=mock)
        result = c.list_services()
        assert result.status == "degraded"

    def test_cross_validate_with_k8s(self):
        c = JaegerDiscoveryClient("http://localhost:16686")
        result = c.cross_validate_with_k8s(
            ["checkout", "payments"], ["checkout", "inventory"],
        )
        assert "checkout" in result["matched"]
        assert "payments" in result["jaeger_only"]
        assert "inventory" in result["k8s_only"]
        assert result["match_ratio"] == 0.5
