"""Jaeger service discovery client.

M2 PR 2.7: JaegerDiscoveryClient for GET /api/services.
Phase 0-8: service discovery only, no complex trace call graph aggregation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx

from packages.common.backend_auth import RedactedBackendAuthConfig, RuntimeBackendAuthConfig


class JaegerDiscoveryError(Exception):
    """Base exception for Jaeger discovery errors."""


class JaegerUnavailableError(JaegerDiscoveryError):
    """Jaeger is unreachable."""


class JaegerAuthError(JaegerDiscoveryError):
    """Authentication error (401/403)."""


@dataclass
class TraceServiceDiscoveryResult:
    """Result from Jaeger service discovery."""
    available_services: list[str] = field(default_factory=list)
    status: str = "unavailable"  # available, degraded, unavailable
    confidence: float = 0.0
    evidence: dict[str, Any] = field(default_factory=dict)
    degraded_reason: str | None = None


class JaegerDiscoveryClient:
    """HTTP client for Jaeger service discovery.

    Phase 0-8: Only GET /api/services. Complex call graph aggregation is
    deferred to Phase 9+.
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

    @property
    def redacted_auth(self) -> RedactedBackendAuthConfig:
        """Return redacted auth for audit/logging (no raw secrets)."""
        if self._auth:
            return self._auth.redacted()
        return RedactedBackendAuthConfig(auth_type="none")

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

    def _get(self, path: str) -> dict[str, Any]:
        if self._client is not None:
            resp = self._client.get(path, timeout=self.timeout)
        else:
            with self._build_client() as client:
                resp = client.get(path)
        if resp.status_code in (401, 403):
            raise JaegerAuthError(f"Auth error: HTTP {resp.status_code}")
        if resp.status_code >= 400:
            raise JaegerUnavailableError(
                f"Error HTTP {resp.status_code}: {resp.text[:200]}"
            )
        return resp.json()  # type: ignore[no-any-return]

    def list_services(self) -> TraceServiceDiscoveryResult:
        """List available services via GET /api/services."""
        try:
            payload = self._get("/api/services")
        except httpx.TimeoutException:
            return TraceServiceDiscoveryResult(
                status="degraded",
                degraded_reason="Jaeger request timed out",
                confidence=0.0,
            )
        except JaegerAuthError:
            return TraceServiceDiscoveryResult(
                status="degraded",
                degraded_reason="Jaeger auth error",
                confidence=0.0,
            )
        except JaegerUnavailableError as exc:
            return TraceServiceDiscoveryResult(
                status="unavailable",
                degraded_reason=str(exc),
                confidence=0.0,
            )
        except Exception as exc:
            return TraceServiceDiscoveryResult(
                status="degraded",
                degraded_reason=f"Jaeger error: {exc}",
                confidence=0.0,
            )

        data = payload.get("data", [])
        if isinstance(data, list):
            service_names = data
        elif isinstance(payload, list):
            service_names = payload
        else:
            service_names = []

        if not service_names:
            return TraceServiceDiscoveryResult(
                status="degraded",
                degraded_reason="No services found in Jaeger",
                confidence=0.5,
            )

        return TraceServiceDiscoveryResult(
            available_services=service_names,
            status="available",
            confidence=0.9,
            evidence={"service_count": len(service_names)},
        )

    def discover_services(self) -> TraceServiceDiscoveryResult:
        """Compatibility wrapper used by DiscoveryRunner."""
        return self.list_services()

    def cross_validate_with_k8s(
        self,
        jaeger_services: list[str],
        k8s_service_names: list[str],
    ) -> dict[str, Any]:
        """Cross-validate Jaeger services with K8s service names."""
        jaeger_set = set(jaeger_services)
        k8s_set = set(k8s_service_names)
        matched = jaeger_set & k8s_set
        jaeger_only = jaeger_set - k8s_set
        k8s_only = k8s_set - jaeger_set
        return {
            "matched": sorted(matched),
            "jaeger_only": sorted(jaeger_only),
            "k8s_only": sorted(k8s_only),
            "match_ratio": len(matched) / max(len(jaeger_set), 1),
            "confidence": 0.8 if len(matched) > 0 else 0.3,
        }
