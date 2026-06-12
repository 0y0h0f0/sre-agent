"""LokiDiscovery — Loki client and log service label detection.

M2 PR 2.3: HTTP client for Loki API + detect_logs_service_label() that
uses label values and sample streams for coverage, not just label keys.
"""

from __future__ import annotations

from typing import Any

import httpx

from packages.common.backend_auth import RuntimeBackendAuthConfig


class LokiClientError(Exception):
    """Base exception for Loki client errors."""


class LokiTimeoutError(LokiClientError):
    """Request timed out."""


class LokiAuthError(LokiClientError):
    """Authentication error (401/403)."""


class LokiResponseError(LokiClientError):
    """Error response from Loki."""


class LokiClient:
    """HTTP client for the Loki HTTP API v1."""

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
            raise LokiAuthError(f"Auth error: HTTP {resp.status_code}")
        if resp.status_code >= 400:
            raise LokiResponseError(f"Error HTTP {resp.status_code}: {resp.text[:200]}")
        return resp.json()  # type: ignore[no-any-return]

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            return self._request("GET", path, params=params)
        except httpx.TimeoutException:
            raise LokiTimeoutError(f"Timeout: {path}") from None
        except (LokiAuthError, LokiResponseError):
            raise

    def list_labels(self) -> list[str]:
        """List all label names via GET /loki/api/v1/labels."""
        payload = self._get("/loki/api/v1/labels")
        data = payload.get("data", [])
        return data if isinstance(data, list) else []

    def list_label_values(self, label_name: str) -> list[str]:
        """List values for a label via GET /loki/api/v1/label/{name}/values."""
        payload = self._get(f"/loki/api/v1/label/{label_name}/values")
        data = payload.get("data", [])
        return data if isinstance(data, list) else []

    def query_range(
        self,
        query: str,
        start: int,
        end: int,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Execute a range query via GET /loki/api/v1/query_range."""
        return self._get(
            "/loki/api/v1/query_range",
            params={"query": query, "start": start, "end": end, "limit": limit},
        )


_CANDIDATE_LOG_LABELS = [
    "service", "app", "job", "container", "deployment",
    "statefulset", "daemonset", "app_kubernetes_io_name",
]
_MIN_COVERAGE_THRESHOLD = 0.80


def detect_logs_service_label(
    client: LokiClient,
    coverage_threshold: float = _MIN_COVERAGE_THRESHOLD,
) -> tuple[str | None, float, dict[str, float]]:
    """Detect which label key best identifies services in Loki log streams.

    Uses label values and sample stream queries for coverage computation,
    NOT just label key existence.

    Returns:
        (selected_label, coverage, all_scores)
    """
    try:
        all_labels = client.list_labels()
    except Exception:
        return None, 0.0, {}

    candidate_labels = [lbl for lbl in _CANDIDATE_LOG_LABELS if lbl in all_labels]
    if not candidate_labels:
        return None, 0.0, {}

    import time
    end = int(time.time())
    start = end - 3600

    scores: dict[str, float] = {}
    for lbl in candidate_labels:
        try:
            values = client.list_label_values(lbl)
        except Exception:
            scores[lbl] = 0.0
            continue
        if not values:
            scores[lbl] = 0.0
            continue

        # Check coverage via sample stream queries for a subset of values.
        sample_values = values[:20]
        streams_found = 0
        for val in sample_values:
            query = f'{{{lbl}="{val}"}}'
            try:
                result = client.query_range(query, start, end, limit=1)
                streams = result.get("data", {}).get("result", [])
                if streams:
                    streams_found += 1
            except Exception:
                pass
        scores[lbl] = streams_found / len(sample_values) if sample_values else 0.0

    if not scores:
        return None, 0.0, {}

    best_label = max(scores, key=scores.get)  # type: ignore[arg-type]
    best_coverage = scores[best_label]

    if best_coverage < coverage_threshold:
        return None, best_coverage, scores
    return best_label, best_coverage, scores
