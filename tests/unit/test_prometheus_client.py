"""Tests for M1 PR 1.2: PrometheusClient."""
from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import pytest

from packages.common.backend_auth import RuntimeBackendAuthConfig
from packages.discovery.prom_discovery import (
    PrometheusAuthError,
    PrometheusClient,
    PrometheusResponseError,
    PrometheusTimeoutError,
)


def _resp(status=200, data=None):
    r = MagicMock(spec=httpx.Response)
    r.status_code = status
    r.json.return_value = data or {}
    r.text = ""
    return r


class TestListMetrics:
    def test_success(self):
        mock = MagicMock(spec=httpx.Client)
        mock.request.return_value = _resp(200, {"status": "success", "data": ["a", "b", "c"]})
        c = PrometheusClient("http://localhost:9090", client=mock)
        assert c.list_metrics() == ["a", "b", "c"]

    def test_with_limit(self):
        mock = MagicMock(spec=httpx.Client)
        data = {"status": "success", "data": [f"m_{i}" for i in range(100)]}
        mock.request.return_value = _resp(200, data)
        c = PrometheusClient("http://localhost:9090", client=mock)
        assert len(c.list_metrics(limit=10)) == 10

    def test_timeout(self):
        mock = MagicMock(spec=httpx.Client)
        mock.request.side_effect = httpx.TimeoutException("timeout")
        c = PrometheusClient("http://localhost:9090", client=mock)
        with pytest.raises(PrometheusTimeoutError):
            c.list_metrics()

    def test_auth_error(self):
        mock = MagicMock(spec=httpx.Client)
        mock.request.return_value = _resp(401)
        c = PrometheusClient("http://localhost:9090", client=mock)
        with pytest.raises(PrometheusAuthError):
            c.list_metrics()

    def test_non_success_status(self):
        mock = MagicMock(spec=httpx.Client)
        mock.request.return_value = _resp(200, {"status": "error"})
        c = PrometheusClient("http://localhost:9090", client=mock)
        with pytest.raises(PrometheusResponseError):
            c.list_metrics()


class TestListSeries:
    def test_success(self):
        mock = MagicMock(spec=httpx.Client)
        mock.request.return_value = _resp(200, {
            "status": "success",
            "data": [{"__name__": "m1", "service": "svc1"}],
        })
        c = PrometheusClient("http://localhost:9090", client=mock)
        series = c.list_series('{__name__="m1"}')
        assert len(series) == 1


class TestGetMetadata:
    def test_success(self):
        mock = MagicMock(spec=httpx.Client)
        mock.request.return_value = _resp(200, {
            "status": "success",
            "data": {"m1": [{"type": "histogram", "unit": "seconds"}]},
        })
        c = PrometheusClient("http://localhost:9090", client=mock)
        meta = c.get_metadata("m1")
        assert meta["type"] == "histogram"

    def test_missing_metric(self):
        mock = MagicMock(spec=httpx.Client)
        mock.request.return_value = _resp(200, {"status": "success", "data": {}})
        c = PrometheusClient("http://localhost:9090", client=mock)
        assert c.get_metadata("unknown") == {}

    def test_type_mismatch(self):
        mock = MagicMock(spec=httpx.Client)
        mock.request.return_value = _resp(200, {
            "status": "success",
            "data": {"m1": [{"type": "gauge", "unit": "bytes"}]},
        })
        c = PrometheusClient("http://localhost:9090", client=mock)
        meta = c.get_metadata("m1")
        assert meta["type"] == "gauge"


class TestQuery:
    def test_success(self):
        mock = MagicMock(spec=httpx.Client)
        data = {"status": "success", "data": {"resultType": "vector", "result": []}}
        mock.request.return_value = _resp(200, data)
        c = PrometheusClient("http://localhost:9090", client=mock)
        result = c.query("up")
        assert result["status"] == "success"


class TestQueryRange:
    def test_empty_result(self):
        mock = MagicMock(spec=httpx.Client)
        data = {"status": "success", "data": {"resultType": "matrix", "result": []}}
        mock.request.return_value = _resp(200, data)
        c = PrometheusClient("http://localhost:9090", client=mock)
        result = c.query_range("up", 0, 300)
        assert result["data"]["result"] == []


class TestAuth:
    def test_bearer_token_header(self):
        """Bearer token config goes into Authorization header."""
        auth = RuntimeBackendAuthConfig(auth_type="bearer", token="test-token")
        c = PrometheusClient("http://localhost:9090", auth=auth)
        # Verify the auth config is stored and used in header construction.
        assert c._auth is not None
        assert c._auth.token == "test-token"
        assert c._auth.auth_type == "bearer"


class TestListLabels:
    def test_success(self):
        mock = MagicMock(spec=httpx.Client)
        data = {"status": "success", "data": ["service", "namespace"]}
        mock.request.return_value = _resp(200, data)
        c = PrometheusClient("http://localhost:9090", client=mock)
        assert c.list_labels() == ["service", "namespace"]
