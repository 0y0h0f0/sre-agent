"""Tests for M2 PR 2.3: LokiDiscovery."""
from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import pytest

from packages.discovery.loki_discovery import (
    LokiClient,
    LokiTimeoutError,
    detect_logs_service_label,
)


def _resp(status=200, data=None):
    r = MagicMock(spec=httpx.Response)
    r.status_code = status
    r.json.return_value = data or {}
    r.text = ""
    return r


class TestLokiClient:
    def test_list_labels_success(self):
        mock = MagicMock(spec=httpx.Client)
        mock.request.return_value = _resp(200, {"data": ["service", "namespace"]})
        c = LokiClient("http://localhost:3100", client=mock)
        assert c.list_labels() == ["service", "namespace"]

    def test_list_label_values_success(self):
        mock = MagicMock(spec=httpx.Client)
        mock.request.return_value = _resp(200, {"data": ["checkout", "payments"]})
        c = LokiClient("http://localhost:3100", client=mock)
        assert c.list_label_values("service") == ["checkout", "payments"]

    def test_timeout(self):
        mock = MagicMock(spec=httpx.Client)
        mock.request.side_effect = httpx.TimeoutException("timeout")
        c = LokiClient("http://localhost:3100", client=mock)
        with pytest.raises(LokiTimeoutError):
            c.list_labels()


class TestDetectLogsServiceLabel:
    def test_keys_only_not_sufficient(self):
        """detect_logs_service_label uses stream queries, not just label keys."""
        mock = MagicMock(spec=httpx.Client)
        # Only label keys exist, but stream queries return empty.
        mock.request.return_value = _resp(200, {"data": ["service", "job"]})
        client = LokiClient("http://localhost:3100", client=mock)
        label, coverage, scores = detect_logs_service_label(client, 0.8)
        assert label is None  # No streams found, despite label keys existing.

    def test_loki_unavailable_degraded(self):
        mock = MagicMock(spec=httpx.Client)
        mock.request.side_effect = httpx.TimeoutException("timeout")
        client = LokiClient("http://localhost:3100", client=mock)
        label, coverage, scores = detect_logs_service_label(client)
        assert label is None
        assert coverage == 0.0
