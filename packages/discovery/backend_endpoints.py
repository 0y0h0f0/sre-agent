"""Observability backend endpoint discovery from K8s services.

M2 PR 2.6: BackendEndpointDetector scans K8s services for Prometheus, Loki,
Jaeger, Alertmanager endpoints. Prefers service DNS over pod IP. Production
backend URL discovery defaults requires_review/detected_only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from packages.common.backend_url_safety import BackendUrlSafetyValidator
from packages.common.settings import get_settings


@dataclass
class BackendEndpoints:
    """Discovered backend endpoint."""
    backend_type: Literal["prometheus", "loki", "jaeger", "alertmanager"]
    url: str
    source: str  # "k8s_service", "manual", "override", "env"
    status: Literal["detected_only", "requires_review", "ready", "degraded", "unavailable"]
    confidence: float = 0.0
    evidence: dict[str, Any] = field(default_factory=dict)
    auth_required_unknown: bool = True
    degraded_reason: str | None = None


# Backend identification patterns: (service_name_contains, default_port).
_BACKEND_PATTERNS: dict[str, list[tuple[str, int, str]]] = {
    "prometheus": [
        ("prometheus", 9090, "http"),
        ("prom", 9090, "http"),
        ("thanos", 9090, "http"),
    ],
    "loki": [
        ("loki", 3100, "http"),
        ("loki-distributed", 3100, "http"),
    ],
    "jaeger": [
        ("jaeger", 16686, "http"),
        ("jaeger-query", 16686, "http"),
    ],
    "alertmanager": [
        ("alertmanager", 9093, "http"),
        ("alertmanager-operated", 9093, "http"),
    ],
}


class BackendEndpointDetector:
    """Scans K8s services for observability backend endpoints."""

    def __init__(
        self,
        url_validator: BackendUrlSafetyValidator | None = None,
        namespace_allowlist: list[str] | None = None,
    ) -> None:
        settings = get_settings()
        self._url_validator = url_validator or BackendUrlSafetyValidator(
            allowlist_patterns=_parse_allowlist(settings.backend_url_allowlist),
            app_env=settings.app_env,
        )
        self._app_env = settings.app_env

    def detect(
        self,
        services: list[Any],  # K8sService list from k8s_discovery
        manual_urls: dict[str, str] | None = None,
    ) -> list[BackendEndpoints]:
        """Detect observability backend endpoints from K8s services.

        manual_urls: backend_type -> URL from env/profile/override (not overridden).
        """
        manual = manual_urls or {}
        endpoints: list[BackendEndpoints] = []

        for backend_type, patterns in _BACKEND_PATTERNS.items():
            if backend_type in manual:
                continue  # Manual config wins.

            best = self._find_backend(services, backend_type, patterns)
            if best is not None:
                endpoints.append(best)
            else:
                # Not found in K8s — record as missing.
                endpoints.append(BackendEndpoints(
                    backend_type=backend_type,  # type: ignore[arg-type]
                    url="",
                    source="k8s_service",
                    status="unavailable" if self._app_env == "production" else "degraded",
                    degraded_reason=f"No {backend_type} service found in K8s",
                ))

        return endpoints

    def _find_backend(
        self,
        services: list[Any],
        backend_type: str,
        patterns: list[tuple[str, int, str]],
    ) -> BackendEndpoints | None:
        for svc in services:
            svc_name = getattr(svc, "name", "")
            namespace = getattr(svc, "namespace", "default")
            for pattern, port, scheme in patterns:
                if pattern in svc_name.lower():
                    service_dns = f"{svc_name}.{namespace}.svc.cluster.local"
                    url = f"{scheme}://{service_dns}:{port}"

                    # Validate URL safety.
                    result = self._url_validator.validate(url)
                    if not result.is_safe:
                        return BackendEndpoints(
                            backend_type=backend_type,  # type: ignore[arg-type]
                            url=url,
                            source="k8s_service",
                            status="degraded",
                            degraded_reason=f"URL safety check failed: {result.reason}",
                            evidence={"service_dns": service_dns},
                        )

                    is_production = self._app_env == "production"
                    return BackendEndpoints(
                        backend_type=backend_type,  # type: ignore[arg-type]
                        url=url,
                        source="k8s_service",
                        status="requires_review" if is_production else "ready",
                        confidence=0.85,
                        evidence={
                            "k8s_service": svc_name,
                            "namespace": namespace,
                            "service_dns": service_dns,
                        },
                        auth_required_unknown=True,
                    )
        return None


def _parse_allowlist(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()] if value else []
