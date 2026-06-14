"""K8sDiscovery — Kubernetes service and workload discovery.

M2 PR 2.1: Discovers namespaces, pods, deployments, statefulsets, daemonsets,
services. Uses lazy-loaded kubernetes client. RBAC failures degrade gracefully.
"""

from __future__ import annotations

import math
import re
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
    uid: str = ""
    owner_references: list[dict[str, Any]] = field(default_factory=list)
    env_service_refs: list[str] = field(default_factory=list)
    config_map_refs: list[str] = field(default_factory=list)


@dataclass
class K8sService:
    """Discovered Kubernetes service."""
    name: str
    namespace: str
    cluster_ip: str = ""
    ports: list[dict[str, Any]] = field(default_factory=list)
    selector: dict[str, str] = field(default_factory=dict)
    labels: dict[str, str] = field(default_factory=dict)
    annotations: dict[str, str] = field(default_factory=dict)


@dataclass
class K8sPod:
    """Sampled Kubernetes pod used for Service -> Workload binding."""
    name: str
    namespace: str
    labels: dict[str, str] = field(default_factory=dict)
    owner_references: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class K8sEndpoint:
    """Discovered Kubernetes Endpoints object for backend endpoint evidence."""
    name: str
    namespace: str
    addresses: list[str] = field(default_factory=list)
    ports: list[dict[str, Any]] = field(default_factory=list)
    labels: dict[str, str] = field(default_factory=dict)


@dataclass
class K8sIngress:
    """Discovered Kubernetes Ingress object for backend endpoint evidence."""
    name: str
    namespace: str
    hosts: list[str] = field(default_factory=list)
    tls_hosts: list[str] = field(default_factory=list)
    service_names: list[str] = field(default_factory=list)
    labels: dict[str, str] = field(default_factory=dict)


@dataclass
class K8sConfigMap:
    """Discovered ConfigMap dependency hints without raw ConfigMap values."""
    name: str
    namespace: str
    service_refs: list[dict[str, str]] = field(default_factory=list)
    labels: dict[str, str] = field(default_factory=dict)


@dataclass
class K8sDiscoveryResult:
    """Result of a K8s discovery run."""
    services: list[K8sService] = field(default_factory=list)
    workloads: list[K8sWorkload] = field(default_factory=list)
    pods: list[K8sPod] = field(default_factory=list)
    endpoints: list[K8sEndpoint] = field(default_factory=list)
    ingresses: list[K8sIngress] = field(default_factory=list)
    config_maps: list[K8sConfigMap] = field(default_factory=list)
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
        pod_sample_ratio: float = 1.0,
    ) -> None:
        settings = get_settings()
        self._namespace_allowlist = namespace_allowlist or _parse_list(
            settings.k8s_namespace
        )
        self._service_allowlist = service_allowlist or []
        self._kube_config_file = kube_config_file
        self._pod_sample_ratio = max(0.0, min(1.0, pod_sample_ratio))
        self._client: Any = None
        self._client_lock = threading.Lock()

    @property
    def _k8s(self) -> Any:
        """Lazily initialize and return kubernetes client modules."""
        if self._client is not None:
            return self._client
        if self._kube_config_file is None and get_settings().k8s_backend != "live":
            raise K8sUnavailableError(
                "Kubernetes discovery is disabled unless K8S_BACKEND=live"
            )
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
                    "networking_v1": client.NetworkingV1Api(),
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
            collectors = [
                ("services", self._list_services, result.services),
                ("pods", self._list_pods, result.pods),
                ("deployments", self._list_deployments, result.workloads),
                ("statefulsets", self._list_statefulsets, result.workloads),
                ("daemonsets", self._list_daemonsets, result.workloads),
                ("endpoints", self._list_endpoints, result.endpoints),
                ("ingresses", self._list_ingresses, result.ingresses),
                ("configmaps", self._list_config_maps, result.config_maps),
            ]
            for label, collector, target in collectors:
                try:
                    target.extend(collector(ns))
                except Exception as exc:
                    result.warnings.append(f"namespace {ns} {label}: {exc}")
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
                annotations=svc.metadata.annotations or {},
            ))
        return result

    def _list_pods(self, namespace: str) -> list[K8sPod]:
        pod_list = self._k8s.core_v1.list_namespaced_pod(namespace)
        items = sorted(
            pod_list.items,
            key=lambda pod: getattr(pod.metadata, "name", ""),
        )
        if self._pod_sample_ratio <= 0:
            items = []
        elif self._pod_sample_ratio < 1.0:
            sample_size = math.ceil(len(items) * self._pod_sample_ratio)
            items = items[:sample_size]
        return [
            K8sPod(
                name=pod.metadata.name,
                namespace=pod.metadata.namespace,
                labels=pod.metadata.labels or {},
                owner_references=_owner_refs(pod.metadata),
            )
            for pod in items
        ]

    def _list_deployments(self, namespace: str) -> list[K8sWorkload]:
        dep_list = self._k8s.apps_v1.list_namespaced_deployment(namespace)
        return [_deployment_to_workload(d) for d in dep_list.items]

    def _list_statefulsets(self, namespace: str) -> list[K8sWorkload]:
        ss_list = self._k8s.apps_v1.list_namespaced_stateful_set(namespace)
        result: list[K8sWorkload] = []
        for ss in ss_list.items:
            result.append(K8sWorkload(
                name=ss.metadata.name, namespace=namespace, kind="StatefulSet",
                labels=_template_labels(ss) or ss.metadata.labels or {},
                selector=ss.spec.selector.match_labels or {},
                replicas=ss.spec.replicas or 0,
                ready_replicas=ss.status.ready_replicas or 0,
                uid=getattr(ss.metadata, "uid", "") or "",
                owner_references=_owner_refs(ss.metadata),
                env_service_refs=_extract_env_service_refs(ss),
                config_map_refs=_extract_config_map_refs(ss),
            ))
        return result

    def _list_daemonsets(self, namespace: str) -> list[K8sWorkload]:
        ds_list = self._k8s.apps_v1.list_namespaced_daemon_set(namespace)
        result: list[K8sWorkload] = []
        for ds in ds_list.items:
            result.append(K8sWorkload(
                name=ds.metadata.name, namespace=namespace, kind="DaemonSet",
                labels=_template_labels(ds) or ds.metadata.labels or {},
                selector=ds.spec.selector.match_labels or {},
                replicas=ds.status.current_number_scheduled or 0,
                ready_replicas=ds.status.number_ready or 0,
                uid=getattr(ds.metadata, "uid", "") or "",
                owner_references=_owner_refs(ds.metadata),
                env_service_refs=_extract_env_service_refs(ds),
                config_map_refs=_extract_config_map_refs(ds),
            ))
        return result

    def _list_endpoints(self, namespace: str) -> list[K8sEndpoint]:
        eps_list = self._k8s.core_v1.list_namespaced_endpoints(namespace)
        result: list[K8sEndpoint] = []
        for eps in eps_list.items:
            addresses: list[str] = []
            ports: list[dict[str, Any]] = []
            for subset in (getattr(eps, "subsets", None) or []):
                for addr in (getattr(subset, "addresses", None) or []):
                    ip = getattr(addr, "ip", None)
                    if ip:
                        addresses.append(ip)
                for port in (getattr(subset, "ports", None) or []):
                    ports.append({
                        "name": getattr(port, "name", None),
                        "port": getattr(port, "port", None),
                        "protocol": getattr(port, "protocol", None),
                    })
            result.append(K8sEndpoint(
                name=eps.metadata.name,
                namespace=eps.metadata.namespace,
                addresses=addresses,
                ports=ports,
                labels=eps.metadata.labels or {},
            ))
        return result

    def _list_ingresses(self, namespace: str) -> list[K8sIngress]:
        networking = getattr(self._k8s, "networking_v1", None)
        if networking is None:
            return []
        ing_list = networking.list_namespaced_ingress(namespace)
        result: list[K8sIngress] = []
        for ing in ing_list.items:
            hosts: list[str] = []
            tls_hosts: list[str] = []
            service_names: list[str] = []
            for tls in (getattr(ing.spec, "tls", None) or []):
                tls_hosts.extend(getattr(tls, "hosts", None) or [])
            for rule in (getattr(ing.spec, "rules", None) or []):
                host = getattr(rule, "host", None)
                if host:
                    hosts.append(host)
                http = getattr(rule, "http", None)
                for path in (getattr(http, "paths", None) or []):
                    backend = getattr(path, "backend", None)
                    service = getattr(backend, "service", None)
                    name = getattr(service, "name", None)
                    if name:
                        service_names.append(name)
            result.append(K8sIngress(
                name=ing.metadata.name,
                namespace=ing.metadata.namespace,
                hosts=sorted(set(hosts)),
                tls_hosts=sorted(set(tls_hosts)),
                service_names=sorted(set(service_names)),
                labels=ing.metadata.labels or {},
            ))
        return result

    def _list_config_maps(self, namespace: str) -> list[K8sConfigMap]:
        cm_list = self._k8s.core_v1.list_namespaced_config_map(namespace)
        result: list[K8sConfigMap] = []
        for cm in cm_list.items:
            service_refs: list[dict[str, str]] = []
            for key, value in (getattr(cm, "data", None) or {}).items():
                for target in _extract_service_refs_from_text(str(value)):
                    service_refs.append({"key": str(key), "target_service": target})
            result.append(K8sConfigMap(
                name=cm.metadata.name,
                namespace=cm.metadata.namespace,
                service_refs=service_refs,
                labels=cm.metadata.labels or {},
            ))
        return result


def _deployment_to_workload(dep: Any) -> K8sWorkload:
    return K8sWorkload(
        name=dep.metadata.name,
        namespace=dep.metadata.namespace,
        kind="Deployment",
        labels=_template_labels(dep) or dep.metadata.labels or {},
        selector=dep.spec.selector.match_labels or {},
        replicas=dep.spec.replicas or 0,
        ready_replicas=dep.status.ready_replicas or 0,
        uid=getattr(dep.metadata, "uid", "") or "",
        owner_references=_owner_refs(dep.metadata),
        env_service_refs=_extract_env_service_refs(dep),
        config_map_refs=_extract_config_map_refs(dep),
    )


def _parse_list(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()] if value else []


def _owner_refs(metadata: Any) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    for ref in (getattr(metadata, "owner_references", None) or []):
        refs.append({
            "kind": getattr(ref, "kind", "") or "",
            "name": getattr(ref, "name", "") or "",
            "uid": getattr(ref, "uid", "") or "",
            "controller": bool(getattr(ref, "controller", False)),
        })
    return refs


def _template_labels(workload: Any) -> dict[str, str]:
    template = getattr(getattr(workload, "spec", None), "template", None)
    metadata = getattr(template, "metadata", None)
    return getattr(metadata, "labels", None) or {}


def _pod_template_spec(workload: Any) -> Any:
    template = getattr(getattr(workload, "spec", None), "template", None)
    return getattr(template, "spec", None)


def _extract_config_map_refs(workload: Any) -> list[str]:
    pod_spec = _pod_template_spec(workload)
    refs: set[str] = set()
    for container in getattr(pod_spec, "containers", None) or []:
        for env in getattr(container, "env", None) or []:
            value_from = getattr(env, "value_from", None)
            cm_ref = getattr(value_from, "config_map_key_ref", None)
            cm_name = getattr(cm_ref, "name", None)
            if cm_name:
                refs.add(cm_name)
        for env_from in getattr(container, "env_from", None) or []:
            cm_ref = getattr(env_from, "config_map_ref", None)
            cm_name = getattr(cm_ref, "name", None)
            if cm_name:
                refs.add(cm_name)
    for volume in getattr(pod_spec, "volumes", None) or []:
        cm_ref = getattr(volume, "config_map", None)
        cm_name = getattr(cm_ref, "name", None)
        if cm_name:
            refs.add(cm_name)
    return sorted(refs)


_SERVICE_DNS_RE = re.compile(
    r"(?:https?://)?"
    r"(?P<service>[a-z0-9]([-a-z0-9]*[a-z0-9])?)"
    r"\.(?P<namespace>[a-z0-9]([-a-z0-9]*[a-z0-9])?)"
    r"\.svc(?:\.cluster\.local)?",
    re.IGNORECASE,
)


def _extract_env_service_refs(workload: Any) -> list[str]:
    pod_spec = _pod_template_spec(workload)
    refs: set[str] = set()
    for container in getattr(pod_spec, "containers", None) or []:
        for env in getattr(container, "env", None) or []:
            name = getattr(env, "name", "") or ""
            value = getattr(env, "value", "") or ""
            for match in _SERVICE_DNS_RE.finditer(value):
                refs.add(f"{match.group('service')}.{match.group('namespace')}")
            if name.endswith("_SERVICE_HOST") and value:
                service = name.removesuffix("_SERVICE_HOST").lower().replace("_", "-")
                refs.add(service)
    return sorted(refs)


def _extract_service_refs_from_text(value: str) -> list[str]:
    refs: set[str] = set()
    for match in _SERVICE_DNS_RE.finditer(value):
        refs.add(f"{match.group('service')}.{match.group('namespace')}")
    return sorted(refs)
