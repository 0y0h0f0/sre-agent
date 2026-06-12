"""Tests for M4 PR 4.1: AlertmanagerClient."""
from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import pytest

from packages.common.backend_auth import RuntimeBackendAuthConfig
from packages.discovery.alertmanager_client import (
    AlertmanagerAuthError,
    AlertmanagerClient,
    AlertmanagerTimeoutError,
)


def _resp(status=200, data=None):
    r = MagicMock(spec=httpx.Response)
    r.status_code = status
    r.json.return_value = data or []
    r.text = ""
    return r


class TestListAlerts:
    def test_success(self):
        mock = MagicMock(spec=httpx.Client)
        mock.request.return_value = _resp(200, [{"fingerprint": "fp1"}])
        c = AlertmanagerClient("http://localhost:9093", client=mock)
        alerts = c.list_alerts()
        assert len(alerts) == 1
        assert alerts[0]["fingerprint"] == "fp1"

    def test_with_filter(self):
        mock = MagicMock(spec=httpx.Client)
        mock.request.return_value = _resp(200, [])
        c = AlertmanagerClient("http://localhost:9093", client=mock)
        alerts = c.list_alerts(filter_matchers=["severity=critical"])
        assert alerts == []

    def test_with_receiver(self):
        mock = MagicMock(spec=httpx.Client)
        mock.request.return_value = _resp(200, [])
        c = AlertmanagerClient("http://localhost:9093", client=mock)
        alerts = c.list_alerts(receiver="team-x")
        assert alerts == []

    def test_timeout(self):
        mock = MagicMock(spec=httpx.Client)
        mock.request.side_effect = httpx.TimeoutException("timeout")
        c = AlertmanagerClient("http://localhost:9093", client=mock)
        with pytest.raises(AlertmanagerTimeoutError):
            c.list_alerts()

    def test_auth_error(self):
        mock = MagicMock(spec=httpx.Client)
        mock.request.return_value = _resp(401)
        c = AlertmanagerClient("http://localhost:9093", client=mock)
        with pytest.raises(AlertmanagerAuthError):
            c.list_alerts()


class TestGetStatus:
    def test_success(self):
        mock = MagicMock(spec=httpx.Client)
        mock.request.return_value = _resp(200, {"versionInfo": {"version": "0.25.0"}})
        c = AlertmanagerClient("http://localhost:9093", client=mock)
        status = c.get_status()
        assert status["versionInfo"]["version"] == "0.25.0"


class TestAuth:
    def test_bearer_token(self):
        """Bearer token auth config is stored correctly."""
        auth = RuntimeBackendAuthConfig(auth_type="bearer", token="tok")
        c = AlertmanagerClient("http://localhost:9093", auth=auth)
        assert c._auth is not None
        assert c._auth.token == "tok"
        assert c._auth.auth_type == "bearer"
