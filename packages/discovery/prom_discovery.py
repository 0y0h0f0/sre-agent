"""Prometheus discovery client and service label detection.

M1 PR 1.2 + 1.4: PrometheusClient for 6 API endpoints, plus
detect_metrics_service_label() for service label discovery.
"""

from __future__ import annotations

from typing import Any

import httpx

from packages.common.backend_auth import RuntimeBackendAuthConfig


class PrometheusClientError(Exception):
    """Base exception for Prometheus client errors."""


class PrometheusTimeoutError(PrometheusClientError):
    """Prometheus request timed out."""


class PrometheusAuthError(PrometheusClientError):
    """Prometheus returned authentication error (401/403)."""


class PrometheusResponseError(PrometheusClientError):
    """Prometheus returned an error response."""


class PrometheusClient:
    """HTTP client for Prometheus HTTP API v1.

    Supports 6 endpoints: label/__name__/values, labels, series, metadata,
    query, query_range. Integrates RuntimeBackendAuthConfig.
    """

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
            elif auth.auth_type == "mtls":
                if auth.cert_file and auth.key_file:
                    kwargs["cert"] = (auth.cert_file, auth.key_file)
                if auth.ca_file:
                    kwargs["verify"] = auth.ca_file
                elif not auth.tls_verify:
                    kwargs["verify"] = False
            if auth.auth_type != "mtls" and not auth.tls_verify:
                kwargs["verify"] = False
        return httpx.Client(**kwargs)

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        if self._client is not None:
            resp = self._client.request(method, path, timeout=self.timeout, **kwargs)
        else:
            with self._build_client() as client:
                resp = client.request(method, path, **kwargs)
        if resp.status_code in (401, 403):
            raise PrometheusAuthError(f"Auth error: HTTP {resp.status_code}")
        if resp.status_code >= 400:
            raise PrometheusResponseError(
                f"Error HTTP {resp.status_code}: {resp.text[:200]}"
            )
        return resp.json()  # type: ignore[no-any-return]

    def _get(
        self, path: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        try:
            return self._request("GET", path, params=params)
        except httpx.TimeoutException:
            raise PrometheusTimeoutError(f"Timeout: {path}") from None
        except (PrometheusAuthError, PrometheusResponseError):
            raise

    # -- API methods --

    def list_metrics(self, limit: int | None = None) -> list[str]:
        payload = self._get("/api/v1/label/__name__/values")
        if payload.get("status") != "success":
            raise PrometheusResponseError(f"status={payload.get('status')}")
        names: list[str] = payload.get("data", [])
        if not isinstance(names, list):
            names = []
        if limit is not None and len(names) > limit:
            names = names[:limit]
        return names

    def list_labels(self) -> list[str]:
        payload = self._get("/api/v1/labels")
        if payload.get("status") != "success":
            raise PrometheusResponseError(f"status={payload.get('status')}")
        labels: list[str] = payload.get("data", [])
        return labels if isinstance(labels, list) else []

    def list_series(self, match: str) -> list[dict[str, str]]:
        payload = self._get("/api/v1/series", params={"match[]": match})
        if payload.get("status") != "success":
            raise PrometheusResponseError(f"status={payload.get('status')}")
        data: list[dict[str, str]] = payload.get("data", [])
        return data if isinstance(data, list) else []

    def get_metadata(self, metric_name: str) -> dict[str, Any]:
        payload = self._get("/api/v1/metadata", params={"metric": metric_name})
        if payload.get("status") != "success":
            raise PrometheusResponseError(f"status={payload.get('status')}")
        metadata_map: dict[str, Any] = payload.get("data", {})
        items: Any = metadata_map.get(metric_name, [])
        if items and isinstance(items, list):
            return items[0]  # type: ignore[no-any-return]
        return {}

    def query(self, promql: str) -> dict[str, Any]:
        return self._get("/api/v1/query", params={"query": promql})

    def query_range(
        self, promql: str, start: int, end: int, step: str = "30s"
    ) -> dict[str, Any]:
        return self._get(
            "/api/v1/query_range",
            params={"query": promql, "start": start, "end": end, "step": step},
        )


# ---------------------------------------------------------------------------
# Service label detection (PR 1.4)
# ---------------------------------------------------------------------------

_CANDIDATE_SERVICE_LABELS: list[str] = [
    "service", "app", "job", "container",
    "deployment", "statefulset", "daemonset", "app_kubernetes_io_name",
]
_MIN_COVERAGE_THRESHOLD = 0.80


def detect_metrics_service_label(
    client: PrometheusClient,
    metric_names: list[str],
    coverage_threshold: float = _MIN_COVERAGE_THRESHOLD,
) -> tuple[str | None, float, dict[str, float]]:
    """Detect which label key best identifies services across metrics.

    Samples up to 200 metrics, checks which candidate label keys appear
    in /series results. Returns the label with highest coverage if it
    meets the threshold.

    Returns:
        (selected_label, coverage, all_scores)
    """
    if not metric_names:
        return None, 0.0, {}

    sample_size = min(len(metric_names), 200)
    sample = metric_names[:sample_size]
    label_counts: dict[str, int] = {lbl: 0 for lbl in _CANDIDATE_SERVICE_LABELS}
    metrics_checked = 0

    for name in sample:
        try:
            series = client.list_series(f'{{__name__="{name}"}}')
        except Exception:
            continue
        metrics_checked += 1
        all_labels: set[str] = set()
        for s in series:
            all_labels.update(s.keys())
        for lbl in _CANDIDATE_SERVICE_LABELS:
            if lbl in all_labels:
                label_counts[lbl] += 1

    if metrics_checked == 0:
        return None, 0.0, {}

    scores = {lbl: cnt / metrics_checked for lbl, cnt in label_counts.items()}
    best_label = max(scores, key=scores.get)  # type: ignore[arg-type]
    best_coverage = scores[best_label]

    if best_coverage < coverage_threshold:
        return None, best_coverage, scores
    return best_label, best_coverage, scores
