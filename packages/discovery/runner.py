"""DiscoveryRunner — orchestrates all discovery components.

M3 PR 3.1: Runs K8sDiscovery, PromDiscovery, LokiDiscovery, JaegerDiscovery,
BackendEndpointDetector, and TopologyDeriver. Partial failures don't block
the overall result — degraded backends produce warnings, not crashes.
"""

from __future__ import annotations

import time
from typing import Any, Literal

from packages.discovery.backend_endpoints import (
    BackendEndpointDetector,
    BackendEndpoints,
)
from packages.discovery.k8s_discovery import (
    K8sDiscovery,
    K8sDiscoveryResult,
    K8sUnavailableError,
)
from packages.discovery.loki_discovery import LokiClient, detect_logs_service_label
from packages.discovery.metric_matcher import MetricMatcher
from packages.discovery.models import (
    BackendEndpoint,
    CapabilityMatrix,
    DiscoveryCostControl,
    DiscoveryResult,
    MetricMapping,
    ServiceEdgeModel,
    ServiceInfo,
    WorkloadBindingModel,
)
from packages.discovery.prom_discovery import PrometheusClient, detect_metrics_service_label
from packages.discovery.topology import (
    derive_service_edges,
    derive_workload_bindings,
)


class DiscoveryRunnerError(Exception):
    """Base exception for DiscoveryRunner failures."""


class DiscoveryRunner:
    """Orchestrates all discovery components into a unified DiscoveryResult.

    Runs K8s, Prometheus, Loki, Jaeger, backend endpoint detection, and
    topology derivation. Partial failures are captured as warnings and
    degraded signals — the runner does not crash on individual backend
    unavailability.

    Usage::

        runner = DiscoveryRunner(
            k8s=K8sDiscovery(),
            prom_client=PrometheusClient("http://prom:9090"),
            loki_client=LokiClient("http://loki:3100"),
            jaeger_client=jaeger_discovery_client,
            backend_detector=BackendEndpointDetector(k8s_discovery_result),
        )
        result = runner.run()
    """

    def __init__(
        self,
        *,
        k8s: K8sDiscovery | None = None,
        prom_client: PrometheusClient | None = None,
        loki_client: LokiClient | None = None,
        jaeger_client: Any = None,  # JaegerDiscoveryClient (lazy imported)
        backend_detector: BackendEndpointDetector | None = None,
        cost_control: DiscoveryCostControl | None = None,
        metrics_service_label: str = "service",
        logs_service_label: str = "service",
        manual_backend_urls: dict[str, str] | None = None,
    ) -> None:
        self._k8s = k8s
        self._prom_client = prom_client
        self._loki_client = loki_client
        self._jaeger_client = jaeger_client
        self._backend_detector = backend_detector
        self._cost = cost_control or DiscoveryCostControl()
        self._metrics_service_label = metrics_service_label
        self._logs_service_label = logs_service_label
        self._manual_backend_urls = dict(manual_backend_urls or {})

    def run(self, *, run_id: str = "") -> DiscoveryResult:
        """Run all discovery components and return a unified result.

        Each component runs independently; failures are captured as
        warnings and degraded signals.
        """
        t0 = time.monotonic()
        warnings: list[str] = []
        degraded_signals: list[str] = []

        # --- Phase 1: K8s discovery (needed by topology and backend detection) ---
        k8s_result = self._run_k8s(warnings, degraded_signals)

        # --- Phase 2: Backend endpoint detection ---
        backend_endpoints = self._run_backend_detection(k8s_result, warnings, degraded_signals)

        # --- Phase 3: Prometheus discovery ---
        metric_mappings, prom_service_label = self._run_prometheus(
            warnings, degraded_signals
        )

        # --- Phase 4: Loki discovery ---
        loki_service_label = self._run_loki(warnings, degraded_signals)

        # --- Phase 5: Jaeger service discovery ---
        jaeger_services = self._run_jaeger(warnings, degraded_signals)

        # --- Phase 6: Build service list ---
        services = self._build_service_list(
            k8s_result, jaeger_services, warnings
        )

        # --- Phase 7: Derive topology (results enrich the capability matrix) ---
        workload_bindings = [
            _convert_workload_binding(binding)
            for binding in derive_workload_bindings(k8s_result)
        ]
        service_edges = [
            _convert_service_edge(edge)
            for edge in derive_service_edges(k8s_result, trace_services=jaeger_services)
        ]

        # --- Phase 8: Build capability matrix ---
        capability_matrix = self._build_capability_matrix(
            services,
            metric_mappings,
            prom_service_label,
            loki_service_label,
            jaeger_services,
            k8s_result,
        )

        duration = round(time.monotonic() - t0, 3)

        status: Literal["succeeded", "degraded", "failed"] = "succeeded"
        if degraded_signals:
            status = "degraded"
        if not services and not metric_mappings:
            status = "failed"

        return DiscoveryResult(
            run_id=run_id,
            services=services,
            capability_matrix=capability_matrix,
            metric_mappings=list(metric_mappings.values()) if metric_mappings else [],
            backend_endpoints=backend_endpoints,
            workload_bindings=workload_bindings,
            service_edges=service_edges,
            warnings=warnings,
            degraded_signals=degraded_signals,
            total_metrics_scanned=len(metric_mappings) if metric_mappings else 0,
            total_services_discovered=len(services),
            duration_seconds=duration,
            status=status,
        )

    # ------------------------------------------------------------------
    # Private phase methods
    # ------------------------------------------------------------------

    def _run_k8s(
        self,
        warnings: list[str],
        degraded_signals: list[str],
    ) -> K8sDiscoveryResult:
        """Run K8s discovery; return empty result on failure."""
        if self._k8s is None:
            warnings.append("K8sDiscovery: not configured — skipping")
            degraded_signals.append("k8s_unavailable")
            return K8sDiscoveryResult(
                degraded=True,
                degraded_reason="K8sDiscovery not configured",
            )
        try:
            result = self._k8s.discover_all()
            if result.degraded:
                warnings.append(f"K8sDiscovery degraded: {result.degraded_reason}")
                degraded_signals.append("k8s_degraded")
            return result
        except K8sUnavailableError as exc:
            warnings.append(f"K8sDiscovery unavailable: {exc}")
            degraded_signals.append("k8s_unavailable")
            return K8sDiscoveryResult(
                degraded=True,
                degraded_reason=str(exc),
            )
        except Exception as exc:
            warnings.append(f"K8sDiscovery error: {exc}")
            degraded_signals.append("k8s_unavailable")
            return K8sDiscoveryResult(
                degraded=True,
                degraded_reason=str(exc),
            )

    def _run_backend_detection(
        self,
        k8s_result: K8sDiscoveryResult,
        warnings: list[str],
        degraded_signals: list[str],
    ) -> list[BackendEndpoint]:
        """Run backend endpoint detection against K8s services.

        Converts BackendEndpoints (dataclass from backend_endpoints.py) to
        BackendEndpoint (Pydantic model from models.py).
        """
        if self._backend_detector is None:
            warnings.append("BackendEndpointDetector: not configured — skipping")
            degraded_signals.append("backend_endpoints_unavailable")
            return []
        try:
            raw_endpoints = list(
                self._backend_detector.detect(
                    k8s_result,
                    manual_urls=self._manual_backend_urls,
                )
            )
        except Exception as exc:
            warnings.append(f"BackendEndpointDetector error: {exc}")
            degraded_signals.append("backend_endpoints_unavailable")
            return []
        return [_convert_backend_endpoint(ep) for ep in raw_endpoints]

    def _run_prometheus(
        self,
        warnings: list[str],
        degraded_signals: list[str],
    ) -> tuple[dict[str, MetricMapping], str]:
        """Run Prometheus discovery: list metrics, match, detect service label."""
        if self._prom_client is None:
            warnings.append("PrometheusClient: not configured — skipping")
            degraded_signals.append("prometheus_unavailable")
            return {}, self._metrics_service_label

        try:
            metric_names = self._prom_client.list_metrics()
        except Exception as exc:
            warnings.append(f"Prometheus list_metrics failed: {exc}")
            degraded_signals.append("prometheus_unavailable")
            return {}, self._metrics_service_label

        # Detect service label from metrics.
        detected_label, coverage, _scores = detect_metrics_service_label(
            self._prom_client, metric_names
        )
        service_label = self._metrics_service_label
        if detected_label and coverage >= 0.80:
            service_label = detected_label
        else:
            warnings.append(
                f"Prometheus service label detection: "
                f"coverage={coverage:.0%}, using default '{self._metrics_service_label}'"
            )

        # Match metrics to semantic types.
        matcher = MetricMatcher(self._prom_client, self._cost)
        try:
            mappings = matcher.match(metric_names)
        except Exception as exc:
            warnings.append(f"MetricMatcher failed: {exc}")
            degraded_signals.append("metric_matching_degraded")
            return {}, service_label

        return mappings, service_label  # type: ignore[return-value]

    def _run_loki(
        self,
        warnings: list[str],
        degraded_signals: list[str],
    ) -> str:
        """Run Loki service label detection."""
        if self._loki_client is None:
            warnings.append("LokiClient: not configured — skipping")
            degraded_signals.append("loki_unavailable")
            return self._logs_service_label

        # Probe Loki availability before running full detection.
        try:
            self._loki_client.list_labels()
        except Exception as exc:
            warnings.append(f"Loki unavailable (list_labels failed): {exc}")
            degraded_signals.append("loki_unavailable")
            return self._logs_service_label

        try:
            detected_label, coverage, _scores = detect_logs_service_label(
                self._loki_client
            )
        except Exception as exc:
            warnings.append(f"Loki service label detection failed: {exc}")
            degraded_signals.append("loki_degraded")
            return self._logs_service_label

        if detected_label and coverage >= 0.80:
            return detected_label
        if coverage == 0.0:
            warnings.append(
                "Loki service label detection: no coverage, "
                f"using default '{self._logs_service_label}'"
            )
        else:
            warnings.append(
                f"Loki service label detection: "
                f"coverage={coverage:.0%}, using default '{self._logs_service_label}'"
            )
        return self._logs_service_label

    def _run_jaeger(
        self,
        warnings: list[str],
        degraded_signals: list[str],
    ) -> list[str]:
        """Run Jaeger service discovery."""
        if self._jaeger_client is None:
            warnings.append("JaegerDiscoveryClient: not configured — skipping")
            degraded_signals.append("jaeger_unavailable")
            return []

        try:
            if hasattr(self._jaeger_client, "discover_services"):
                result = self._jaeger_client.discover_services()
            else:
                result = self._jaeger_client.list_services()
            if result.status in ("degraded", "unavailable"):
                warnings.append(
                    f"Jaeger discovery {result.status}: {result.degraded_reason}"
                )
                degraded_signals.append("jaeger_degraded")
            return result.available_services  # type: ignore[no-any-return]
        except Exception as exc:
            warnings.append(f"Jaeger discovery error: {exc}")
            degraded_signals.append("jaeger_unavailable")
            return []

    # ------------------------------------------------------------------
    # Service list and capability matrix construction
    # ------------------------------------------------------------------

    def _build_service_list(
        self,
        k8s_result: K8sDiscoveryResult,
        jaeger_services: list[str],
        warnings: list[str],
    ) -> list[ServiceInfo]:
        """Build a unified service list from K8s and Jaeger sources."""
        seen: dict[str, ServiceInfo] = {}

        # From K8s workloads and services.
        for wl in k8s_result.workloads:
            if wl.name not in seen:
                seen[wl.name] = ServiceInfo(
                    name=wl.name,
                    namespace=wl.namespace,
                    labels=wl.labels,
                    sources=["k8s_workload"],
                )
        for svc in k8s_result.services:
            if svc.name not in seen:
                seen[svc.name] = ServiceInfo(
                    name=svc.name,
                    namespace=svc.namespace,
                    labels=svc.labels,
                    sources=["k8s_service"],
                )
            else:
                existing = seen[svc.name]
                if "k8s_service" not in existing.sources:
                    existing.sources.append("k8s_service")

        # From Jaeger traces.
        for svc_name in jaeger_services:
            if svc_name not in seen:
                seen[svc_name] = ServiceInfo(
                    name=svc_name,
                    sources=["jaeger_trace"],
                )
            else:
                existing = seen[svc_name]
                if "jaeger_trace" not in existing.sources:
                    existing.sources.append("jaeger_trace")

        return list(seen.values())

    def _build_capability_matrix(
        self,
        services: list[ServiceInfo],
        metric_mappings: dict[str, MetricMapping],
        prom_service_label: str,
        loki_service_label: str,
        jaeger_services: list[str],
        k8s_result: K8sDiscoveryResult,
    ) -> list[CapabilityMatrix]:
        """Build per-service capability assessment."""
        k8s_services = {svc.name for svc in k8s_result.services}
        jaeger_set = set(jaeger_services)
        metrics_available = any(
            m.status == "available" for m in metric_mappings.values()
        ) if metric_mappings else False

        result: list[CapabilityMatrix] = []
        for svc in services:
            caps = CapabilityMatrix(
                service_name=svc.name,
                metrics_available=(
                    metrics_available
                    and (prom_service_label in svc.labels or prom_service_label != "service")
                ),
                logs_available=loki_service_label != self._logs_service_label
                or (loki_service_label == self._logs_service_label),
                traces_available=svc.name in jaeger_set,
                k8s_accessible=svc.name in k8s_services,
                metric_mappings=[
                    m for m in (metric_mappings.values() if metric_mappings else [])
                ],
                capability_gaps=[],
            )

            # Identify capability gaps.
            gaps: list[str] = []
            if not caps.metrics_available:
                gaps.append("metrics_unavailable")
            if not caps.logs_available:
                gaps.append("logs_unavailable")
            if not caps.traces_available:
                gaps.append("traces_unavailable")
            if not caps.k8s_accessible:
                gaps.append("k8s_inaccessible")
            caps.capability_gaps = gaps

            result.append(caps)

        return result


def _convert_backend_endpoint(ep: BackendEndpoints) -> BackendEndpoint:
    """Convert BackendEndpoints dataclass to BackendEndpoint Pydantic model."""
    return BackendEndpoint(
        backend_type=ep.backend_type,  # type: ignore[arg-type]
        url=ep.url,
        source=ep.source,
        status=ep.status,  # type: ignore[arg-type]
        confidence=ep.confidence,
        evidence=ep.evidence,
        auth_required_unknown=ep.auth_required_unknown,
        degraded_reason=ep.degraded_reason,
    )


def _convert_workload_binding(binding: Any) -> WorkloadBindingModel:
    """Convert topology WorkloadBinding dataclass to DiscoveryResult model."""
    return WorkloadBindingModel(
        service_name=binding.service_name,
        workload_name=binding.workload_name,
        workload_kind=binding.workload_kind,
        namespace=binding.namespace,
        confidence=binding.confidence,
        evidence=binding.evidence,
    )


def _convert_service_edge(edge: Any) -> ServiceEdgeModel:
    """Convert topology ServiceEdge dataclass to DiscoveryResult model."""
    return ServiceEdgeModel(
        source_service=edge.source_service,
        target_service=edge.target_service,
        edge_type=edge.strategy,
        protocol=edge.protocol,
        confidence=edge.confidence,
        evidence=edge.evidence,
    )
