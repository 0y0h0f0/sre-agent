"""K8sDiscovery — Kubernetes service and workload discovery.

M2 PR 2.1: Discovers namespaces, pods, deployments, statefulsets, daemonsets,
services. Uses lazy-loaded kubernetes client. RBAC failures degrade gracefully.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any

from packages.common.settings import get_settings


class K8sDiscoveryError(Exception):
    """Base exception for K8s discovery errors."""


class K8sUnavailableError(K8sDiscoveryError):
    """K8s API is unavailable (not running in cluster, RBAC insufficient)."""


@dataclass
class K8sWorkload:
    """Discovered Kubernetes workload."""
    name: str
    namespace: str
    kind: str
    labels: dict[str, str] = field(default_factory=dict)
    selector: dict[str, str] = field(default_factory=dict)
    replicas: int = 0
    ready_replicas: int = 0


@dataclass
class K8sService:
    """Discovered Kubernetes service."""
    name: str
    namespace: str
    cluster_ip: str = ""
    ports: list[dict[str, Any]] = field(default_factory=list)
    selector: dict[str, str] = field(default_factory=dict)
    labels: dict[str, str] = field(default_factory=dict)


@dataclass
class K8sDiscoveryResult:
    """Result of a K8s discovery run."""
    services: list[K8sService] = field(default_factory=list)
    workloads: list[K8sWorkload] = field(default_factory=list)
    namespaces: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    degraded: bool = False
    degraded_reason: str | None = None


class K8sDiscovery:
    """Discovers Kubernetes services and workloads.

    The kubernetes Python package is lazily loaded on first use.
    Initialization is thread-safe with import failure caching (TTL 30s).
    """

    _import_lock: threading.Lock = threading.Lock()
    _import_failed_at: float | None = None
    _import_failure_ttl: float = 30.0

    def __init__(
        self,
        namespace_allowlist: list[str] | None = None,
        service_allowlist: list[str] | None = None,
        kube_config_file: str | None = None,
    ) -> None:
        settings = get_settings()
        self._namespace_allowlist = namespace_allowlist or _parse_list(
            settings.k8s_namespace
        )
        self._service_allowlist = service_allowlist or []
        self._kube_config_file = kube_config_file
        self._client: Any = None
        self._client_lock = threading.Lock()

    @property
    def _k8s(self) -> Any:
        """Lazily initialize and return kubernetes client modules."""
        if self._kube_config_file is None and get_settings().k8s_backend != "live":
            raise K8sUnavailableError(
                "Kubernetes discovery is disabled unless K8S_BACKEND=live"
            )
        if self._client is not None:
            return self._client
        with self._client_lock:
            if self._client is not None:
                return self._client
            with K8sDiscovery._import_lock:
                if K8sDiscovery._import_failed_at is not None:
                    elapsed = time.time() - K8sDiscovery._import_failed_at
                    if elapsed < K8sDiscovery._import_failure_ttl:
                        raise K8sUnavailableError(
                            f"kubernetes package unavailable (cached, {elapsed:.0f}s ago)"
                        )
            try:
                from kubernetes import client, config  # type: ignore
                if self._kube_config_file:
                    config.load_kube_config(config_file=self._kube_config_file)
                else:
                    try:
                        config.load_incluster_config()
                    except config.ConfigException:
                        config.load_kube_config()
                self._client = type("K8sModules", (), {
                    "core_v1": client.CoreV1Api(),
                    "apps_v1": client.AppsV1Api(),
                })()
                return self._client
            except ImportError:
                with K8sDiscovery._import_lock:
                    K8sDiscovery._import_failed_at = time.time()
                raise K8sUnavailableError(
                    "kubernetes Python package not installed"
                ) from None
            except Exception as exc:
                raise K8sUnavailableError(
                    f"Failed to initialize K8s client: {exc}"
                ) from exc

    def discover_all(self) -> K8sDiscoveryResult:
        """Run full K8s discovery across all allowed namespaces."""
        result = K8sDiscoveryResult()
        try:
            namespaces = self._list_namespaces()
            result.namespaces = namespaces
        except K8sUnavailableError as exc:
            result.degraded = True
            result.degraded_reason = str(exc)
            return result
        for ns in namespaces:
            try:
                result.services.extend(self._list_services(ns))
                result.workloads.extend(self._list_deployments(ns))
                result.workloads.extend(self._list_statefulsets(ns))
                result.workloads.extend(self._list_daemonsets(ns))
            except Exception as exc:
                result.warnings.append(f"namespace {ns}: {exc}")
        if not result.services and not result.workloads:
            result.degraded = True
            result.degraded_reason = "No services or workloads discovered"
        return result

    def _list_namespaces(self) -> list[str]:
        try:
            ns_list = self._k8s.core_v1.list_namespace()
            names = [ns.metadata.name for ns in ns_list.items]
        except Exception as exc:
            if "403" in str(exc) or "Forbidden" in str(exc):
                # Fall back to allowlist if cluster-wide namespace listing is
                # forbidden by RBAC — the operator knows which namespaces to
                # target and has configured them in K8S_NAMESPACE.
                if self._namespace_allowlist:
                    return self._namespace_allowlist
                raise K8sUnavailableError("RBAC forbidden") from exc
            raise K8sUnavailableError(
                f"K8s namespace discovery failed: {exc}"
            ) from exc
        if self._namespace_allowlist:
            names = [n for n in names if n in self._namespace_allowlist]
        return names

    def _list_services(self, namespace: str) -> list[K8sService]:
        svc_list = self._k8s.core_v1.list_namespaced_service(namespace)
        result: list[K8sService] = []
        for svc in svc_list.items:
            name = svc.metadata.name
            if self._service_allowlist and name not in self._service_allowlist:
                continue
            result.append(K8sService(
                name=name, namespace=namespace,
                cluster_ip=svc.spec.cluster_ip or "",
                ports=[{"name": p.name, "port": p.port, "protocol": p.protocol}
                       for p in (svc.spec.ports or [])],
                selector=svc.spec.selector or {},
                labels=svc.metadata.labels or {},
            ))
        return result

    def _list_deployments(self, namespace: str) -> list[K8sWorkload]:
        dep_list = self._k8s.apps_v1.list_namespaced_deployment(namespace)
        return [_deployment_to_workload(d) for d in dep_list.items]

    def _list_statefulsets(self, namespace: str) -> list[K8sWorkload]:
        ss_list = self._k8s.apps_v1.list_namespaced_stateful_set(namespace)
        result: list[K8sWorkload] = []
        for ss in ss_list.items:
            result.append(K8sWorkload(
                name=ss.metadata.name, namespace=namespace, kind="StatefulSet",
                labels=ss.metadata.labels or {},
                selector=ss.spec.selector.match_labels or {},
                replicas=ss.spec.replicas or 0,
                ready_replicas=ss.status.ready_replicas or 0,
            ))
        return result

    def _list_daemonsets(self, namespace: str) -> list[K8sWorkload]:
        ds_list = self._k8s.apps_v1.list_namespaced_daemon_set(namespace)
        result: list[K8sWorkload] = []
        for ds in ds_list.items:
            result.append(K8sWorkload(
                name=ds.metadata.name, namespace=namespace, kind="DaemonSet",
                labels=ds.metadata.labels or {},
                selector=ds.spec.selector.match_labels or {},
                replicas=ds.status.current_number_scheduled or 0,
                ready_replicas=ds.status.number_ready or 0,
            ))
        return result


def _deployment_to_workload(dep: Any) -> K8sWorkload:
    return K8sWorkload(
        name=dep.metadata.name,
        namespace=dep.metadata.namespace,
        kind="Deployment",
        labels=dep.metadata.labels or {},
        selector=dep.spec.selector.match_labels or {},
        replicas=dep.spec.replicas or 0,
        ready_replicas=dep.status.ready_replicas or 0,
    )


def _parse_list(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()] if value else []
