"""Alertmanager HTTP client for alert polling.

M4 PR 4.1: AlertmanagerClient supporting GET /api/v2/alerts and /api/v2/status.
Receiver is passed as independent query parameter, not as label matcher.
"""

from __future__ import annotations

from typing import Any

import httpx

from packages.common.backend_auth import RuntimeBackendAuthConfig


class AlertmanagerClientError(Exception):
    """Base exception for Alertmanager client errors."""


class AlertmanagerTimeoutError(AlertmanagerClientError):
    """Request timed out."""


class AlertmanagerAuthError(AlertmanagerClientError):
    """Authentication error (401/403)."""


class AlertmanagerResponseError(AlertmanagerClientError):
    """Error response from Alertmanager."""


class AlertmanagerClient:
    """HTTP client for the Alertmanager HTTP API v2."""

    def __init__(
        self,
        base_url: str,
        auth: RuntimeBackendAuthConfig | None = None,
        timeout: float = 10.0,
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._client = client
        self._auth = auth

    def _build_client(self) -> httpx.Client:
        kwargs: dict[str, Any] = {"base_url": self.base_url, "timeout": self.timeout}
        auth = self._auth
        if auth is not None:
            if auth.auth_type == "bearer" and auth.token:
                kwargs["headers"] = {"Authorization": f"Bearer {auth.token}"}
            elif auth.auth_type == "basic" and auth.username and auth.password:
                kwargs["auth"] = (auth.username, auth.password)
            if not auth.tls_verify:
                kwargs["verify"] = False
        return httpx.Client(**kwargs)

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        if self._client is not None:
            resp = self._client.request(method, path, timeout=self.timeout, **kwargs)
        else:
            with self._build_client() as client:
                resp = client.request(method, path, **kwargs)
        if resp.status_code in (401, 403):
            raise AlertmanagerAuthError(f"Auth error: HTTP {resp.status_code}")
        if resp.status_code >= 400:
            raise AlertmanagerResponseError(
                f"Error HTTP {resp.status_code}: {resp.text[:200]}"
            )
        return resp.json()  # type: ignore[no-any-return]

    def _get(
        self, path: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        try:
            return self._request("GET", path, params=params)
        except httpx.TimeoutException:
            raise AlertmanagerTimeoutError(f"Timeout: {path}") from None
        except (AlertmanagerAuthError, AlertmanagerResponseError):
            raise

    def list_alerts(
        self,
        filter_matchers: list[str] | None = None,
        receiver: str | None = None,
    ) -> list[dict[str, Any]]:
        """List alerts via GET /api/v2/alerts.

        filter_matchers: label matchers passed as filter[] query params.
        receiver: Alertmanager receiver passed as receiver query param (not matcher).
        """
        params: dict[str, Any] = {}
        if filter_matchers:
            params["filter"] = filter_matchers
        if receiver:
            params["receiver"] = receiver
        payload = self._get("/api/v2/alerts", params=params or None)
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            raw: Any = payload.get("data", payload.get("alerts", []))
            if isinstance(raw, list):
                return raw
        return []

    def get_status(self) -> dict[str, Any]:
        """Get Alertmanager status via GET /api/v2/status."""
        payload = self._get("/api/v2/status")
        return payload if isinstance(payload, dict) else {}
