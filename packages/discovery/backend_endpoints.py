"""Observability backend endpoint discovery from K8s services.

M2 PR 2.6: BackendEndpointDetector scans K8s services for Prometheus, Loki,
Jaeger, Alertmanager endpoints. Prefers service DNS over pod IP. Production
backend URL discovery defaults requires_review/detected_only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from packages.common.backend_url_safety import BackendUrlSafetyValidator
from packages.common.feature_flags import is_m9_subfeature_enabled
from packages.common.settings import get_settings


@dataclass
class BackendEndpoints:
    """Discovered backend endpoint."""
    backend_type: Literal["prometheus", "loki", "jaeger", "alertmanager", "tempo"]
    url: str
    source: str  # "k8s_service", "manual", "override", "env"
    status: Literal[
        "detected_only", "requires_review", "ready", "degraded", "unavailable", "rejected"
    ]
    confidence: float = 0.0
    evidence: dict[str, Any] = field(default_factory=dict)
    auth_required_unknown: bool = True
    degraded_reason: str | None = None


@dataclass
class _BackendCandidate:
    backend_type: str
    url: str
    source: str
    confidence: float
    evidence: dict[str, Any] = field(default_factory=dict)
    auth_required_unknown: bool = True


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
    "tempo": [
        ("tempo", 3200, "http"),
        ("tempo-query", 3200, "http"),
        ("tempo-distributed", 3200, "http"),
        ("tempo-distributor", 3200, "http"),
    ],
}


class BackendEndpointDetector:
    """Scans K8s services for observability backend endpoints."""

    def __init__(
        self,
        url_validator: BackendUrlSafetyValidator | Any | None = None,
        namespace_allowlist: list[str] | None = None,
    ) -> None:
        settings = get_settings()
        self._initial_services: Any = []
        self._namespace_allowlist = set(namespace_allowlist or [])
        if url_validator is not None and not hasattr(url_validator, "validate"):
            # Backward compatibility: older worker code passed a service list as
            # the first positional argument; older docs also showed passing the
            # full K8sDiscoveryResult.
            if hasattr(url_validator, "services"):
                self._initial_services = url_validator
            else:
                self._initial_services = list(url_validator)
            url_validator = None
        self._url_validator = url_validator or BackendUrlSafetyValidator(
            allowlist_patterns=_parse_allowlist(settings.backend_url_allowlist),
            app_env=settings.app_env,
        )
        self._app_env = settings.app_env

    def detect(
        self,
        services: list[Any] | Any | None = None,
        endpoints: list[Any] | None = None,
        ingresses: list[Any] | None = None,
        manual_urls: dict[str, str] | None = None,
    ) -> list[BackendEndpoints]:
        """Detect observability backend endpoints from K8s services.

        manual_urls: backend_type -> URL from env/profile/override (not overridden).
        """
        services, endpoint_items, ingress_items, degraded_reason = _normalize_inputs(
            services if services is not None else self._initial_services,
            endpoints,
            ingresses,
        )
        services = _filter_by_namespace(services, self._namespace_allowlist)
        endpoint_items = _filter_by_namespace(endpoint_items, self._namespace_allowlist)
        ingress_items = _filter_by_namespace(ingress_items, self._namespace_allowlist)
        manual = manual_urls or {}
        discovered: list[BackendEndpoints] = []

        for backend_type, patterns in _BACKEND_PATTERNS.items():
            if backend_type in manual:
                continue  # Manual config wins.

            # M9 gate: tempo requires feature flag.
            if backend_type == "tempo" and not is_m9_subfeature_enabled(
                get_settings(), "tempo_discovery"
            ):
                continue

            best = self._find_backend(
                services,
                backend_type,
                patterns,
                endpoints_by_name=_index_by_name(endpoint_items),
                ingresses=ingress_items,
            )
            if best is not None:
                discovered.append(best)
            else:
                # Not found in K8s — record as missing.
                discovered.append(BackendEndpoints(
                    backend_type=backend_type,  # type: ignore[arg-type]
                    url="",
                    source="k8s_service",
                    status="unavailable" if self._app_env == "production" else "degraded",
                    degraded_reason=(
                        degraded_reason
                        or f"No {backend_type} service found in K8s"
                    ),
                ))

        return discovered

    def _find_backend(
        self,
        services: list[Any],
        backend_type: str,
        patterns: list[tuple[str, int, str]],
        *,
        endpoints_by_name: dict[tuple[str, str], Any],
        ingresses: list[Any],
    ) -> BackendEndpoints | None:
        candidates: list[_BackendCandidate] = []
        for svc in services:
            svc_name = getattr(svc, "name", "") or ""
            namespace = getattr(svc, "namespace", "default")
            for pattern, port, scheme in patterns:
                if not _matches_backend(svc, pattern):
                    continue
                service_dns = f"{svc_name}.{namespace}.svc.cluster.local"
                selected_port, port_evidence = _select_port(svc, port)
                url = f"{scheme}://{service_dns}:{selected_port}"
                endpoint = endpoints_by_name.get((namespace, svc_name))
                confidence = 0.85
                if port_evidence.get("matched_expected_port"):
                    confidence += 0.05
                elif port_evidence.get("selected_from_service"):
                    confidence -= 0.15
                if endpoint is not None and getattr(endpoint, "addresses", []):
                    confidence += 0.05
                evidence: dict[str, Any] = {
                    "k8s_service": svc_name,
                    "namespace": namespace,
                    "service_dns": service_dns,
                    "port": selected_port,
                    **port_evidence,
                }
                if endpoint is not None:
                    evidence["endpoint"] = {
                        "name": getattr(endpoint, "name", ""),
                        "address_count": len(getattr(endpoint, "addresses", []) or []),
                        "ports": getattr(endpoint, "ports", []) or [],
                    }
                related_ingresses = [
                    ing for ing in ingresses
                    if getattr(ing, "namespace", namespace) == namespace
                    and svc_name in (getattr(ing, "service_names", []) or [])
                ]
                if related_ingresses:
                    evidence["ingresses"] = [
                        {
                            "name": getattr(ing, "name", ""),
                            "hosts": getattr(ing, "hosts", []) or [],
                        }
                        for ing in related_ingresses
                    ]
                candidates.append(_BackendCandidate(
                    backend_type=backend_type,
                    url=url,
                    source="k8s_service",
                    confidence=min(confidence, 0.95),
                    evidence=evidence,
                ))

        candidates.extend(_ingress_only_candidates(backend_type, patterns, ingresses))
        candidates = _dedupe_candidates(candidates)
        if not candidates:
            return None

        candidates.sort(key=lambda candidate: candidate.confidence, reverse=True)
        best = candidates[0]

        result = self._url_validator.validate(best.url)
        if not result.is_safe:
            final_status: Literal["degraded", "rejected"] = (
                "rejected" if backend_type == "tempo" else "degraded"
            )
            return BackendEndpoints(
                backend_type=backend_type,  # type: ignore[arg-type]
                url=best.url,
                source=best.source,
                status=final_status,
                confidence=best.confidence,
                degraded_reason=f"URL safety check failed: {result.reason}",
                evidence=best.evidence,
                auth_required_unknown=best.auth_required_unknown,
            )

        evidence = dict(best.evidence)
        if len(candidates) > 1:
            evidence["candidates"] = [
                {
                    "url": candidate.url,
                    "source": candidate.source,
                    "confidence": candidate.confidence,
                    "evidence": candidate.evidence,
                }
                for candidate in candidates
            ]

        return BackendEndpoints(
            backend_type=backend_type,  # type: ignore[arg-type]
            url=best.url,
            source=best.source,
            status=self._status_for_candidate(best, candidates),
            confidence=best.confidence,
            evidence=evidence,
            auth_required_unknown=best.auth_required_unknown,
        )

    def _status_for_candidate(
        self,
        candidate: _BackendCandidate,
        candidates: list[_BackendCandidate],
    ) -> Literal["detected_only", "requires_review", "ready"]:
        if candidate.confidence <= 0.70:
            return "detected_only"
        if len(candidates) > 1:
            return "requires_review"
        if self._app_env == "production":
            return "requires_review"
        if candidate.auth_required_unknown:
            return "requires_review"
        return "ready"


def _normalize_inputs(
    services_or_result: list[Any] | Any,
    endpoints: list[Any] | None,
    ingresses: list[Any] | None,
) -> tuple[list[Any], list[Any], list[Any], str | None]:
    if hasattr(services_or_result, "services"):
        result = services_or_result
        degraded_reason = None
        if getattr(result, "degraded", False):
            degraded_reason = getattr(result, "degraded_reason", None)
        return (
            list(getattr(result, "services", []) or []),
            list(endpoints or getattr(result, "endpoints", []) or []),
            list(ingresses or getattr(result, "ingresses", []) or []),
            degraded_reason,
        )
    return list(services_or_result or []), list(endpoints or []), list(ingresses or []), None


def _filter_by_namespace(items: list[Any], allowlist: set[str]) -> list[Any]:
    if not allowlist:
        return items
    return [
        item for item in items
        if getattr(item, "namespace", "default") in allowlist
    ]


def _matches_backend(service: Any, pattern: str) -> bool:
    name = (getattr(service, "name", "") or "").lower()
    if pattern in name:
        return True
    labels = getattr(service, "labels", {}) or {}
    annotations = getattr(service, "annotations", {}) or {}
    values = [*labels.values(), *annotations.values()]
    haystack = " ".join(str(value).lower() for value in values)
    return pattern in haystack


def _select_port(service: Any, expected_port: int) -> tuple[int, dict[str, Any]]:
    ports = getattr(service, "ports", []) or []
    for port in ports:
        number = _port_number(port)
        name = str(_port_name(port) or "").lower()
        if number == expected_port or _port_name_matches(name, expected_port):
            return expected_port if number is None else number, {
                "matched_expected_port": True,
                "selected_port_name": name,
                "service_ports": ports,
            }
    if ports:
        selected = _port_number(ports[0])
        if selected is not None:
            return selected, {
                "selected_from_service": True,
                "expected_port": expected_port,
                "service_ports": ports,
            }
    return expected_port, {
        "defaulted_port": True,
        "expected_port": expected_port,
        "service_ports": ports,
    }


def _port_number(port: Any) -> int | None:
    if isinstance(port, dict):
        value = port.get("port")
    else:
        value = getattr(port, "port", None)
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _port_name(port: Any) -> str | None:
    if isinstance(port, dict):
        return port.get("name")
    return getattr(port, "name", None)


def _port_name_matches(name: str, expected_port: int) -> bool:
    if expected_port == 9090:
        return "prom" in name or "http" in name
    if expected_port == 3100:
        return "loki" in name or "http" in name
    if expected_port == 16686:
        return "query" in name or "http" in name
    if expected_port == 9093:
        return "web" in name or "http" in name
    if expected_port == 3200:
        return "tempo" in name or "http" in name
    return False


def _index_by_name(items: list[Any]) -> dict[tuple[str, str], Any]:
    return {
        (getattr(item, "namespace", "default"), getattr(item, "name", "")): item
        for item in items
    }


def _ingress_only_candidates(
    backend_type: str,
    patterns: list[tuple[str, int, str]],
    ingresses: list[Any],
) -> list[_BackendCandidate]:
    candidates: list[_BackendCandidate] = []
    for ing in ingresses:
        namespace = getattr(ing, "namespace", "default")
        hosts = getattr(ing, "hosts", []) or []
        if not hosts:
            continue
        tls_hosts = set(getattr(ing, "tls_hosts", []) or [])
        service_names = getattr(ing, "service_names", []) or []
        for service_name in service_names:
            for pattern, _port, _scheme in patterns:
                if pattern not in service_name.lower() and not any(
                    pattern in host.lower() for host in hosts
                ):
                    continue
                host = hosts[0]
                scheme = "https" if host in tls_hosts else "http"
                candidates.append(_BackendCandidate(
                    backend_type=backend_type,
                    url=f"{scheme}://{host}",
                    source="k8s_ingress",
                    confidence=0.75,
                    evidence={
                        "ingress": getattr(ing, "name", ""),
                        "namespace": namespace,
                        "hosts": hosts,
                        "service_name": service_name,
                    },
                ))
    return candidates


def _dedupe_candidates(candidates: list[_BackendCandidate]) -> list[_BackendCandidate]:
    by_url: dict[str, _BackendCandidate] = {}
    for candidate in candidates:
        existing = by_url.get(candidate.url)
        if existing is None or candidate.confidence > existing.confidence:
            by_url[candidate.url] = candidate
    return list(by_url.values())


def _parse_allowlist(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()] if value else []
