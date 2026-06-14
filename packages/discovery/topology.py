"""Topology derivation — WorkloadBinding and ServiceEdge.

M2 PR 2.4 + 2.5:
- WorkloadBinding: Service selector -> Pod labels -> ownerRef -> Workload
- ServiceEdge: Four strategies by confidence (manual > trace > env > configmap)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from packages.discovery.k8s_discovery import K8sDiscoveryResult


@dataclass
class WorkloadBinding:
    """Binds a K8s Service to its backing Workload."""
    service_name: str
    workload_name: str
    workload_kind: str
    namespace: str
    confidence: float = 1.0
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass
class ServiceEdge:
    """A directed edge between two services."""
    source_service: str
    target_service: str
    protocol: str = "unknown"
    confidence: float = 0.5
    strategy: Literal["manual", "trace", "env", "configmap"] = "env"
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass
class ServiceReference:
    """Weak dependency evidence found in env vars or ConfigMap content."""
    source_service: str
    target_service: str
    evidence: dict[str, Any] = field(default_factory=dict)


def derive_workload_bindings(
    k8s_result: K8sDiscoveryResult,
) -> list[WorkloadBinding]:
    """Derive WorkloadBinding: Service selector -> workload via ownerRefs.

    Does NOT produce ServiceEdge. Only workload bindings.
    """
    bindings: list[WorkloadBinding] = []
    workloads_by_key = {
        (wl.namespace, wl.kind, wl.name): wl for wl in k8s_result.workloads
    }

    for svc in k8s_result.services:
        if not svc.selector:
            continue
        matching_pods = [
            pod for pod in k8s_result.pods
            if pod.namespace == svc.namespace
            and _selector_matches(svc.selector, pod.labels)
        ]
        for pod in matching_pods:
            workload = _workload_from_owner_refs(
                pod.owner_references,
                workloads_by_key,
                pod.namespace,
            )
            if workload is None:
                continue
            binding = WorkloadBinding(
                service_name=svc.name,
                workload_name=workload.name,
                workload_kind=workload.kind,
                namespace=svc.namespace,
                evidence={
                    "selector": svc.selector,
                    "pod": pod.name,
                    "pod_labels": pod.labels,
                    "owner_references": pod.owner_references,
                },
            )
            key = (
                binding.service_name,
                binding.workload_name,
                binding.workload_kind,
                binding.namespace,
            )
            if key not in {
                (b.service_name, b.workload_name, b.workload_kind, b.namespace)
                for b in bindings
            }:
                bindings.append(binding)
                break
    return bindings


def derive_service_edges(
    k8s_result: K8sDiscoveryResult,
    manual_edges: list[ServiceEdge] | None = None,
    trace_edges: list[ServiceEdge] | None = None,
    env_references: list[ServiceReference] | None = None,
    configmap_references: list[ServiceReference] | None = None,
    trace_services: list[str] | None = None,
) -> list[ServiceEdge]:
    """Derive ServiceEdge list using four strategies by confidence.

    Priority: manual (1.0) > trace (0.8-0.95) > env (0.5-0.7) > configmap (0.4-0.7).
    Conflicting edges: higher confidence wins.

    ``trace_services`` is accepted for backward compatibility with older
    callers. A bare service list proves trace availability, not a call graph,
    so it does not create ServiceEdge records by itself.
    """
    edges: dict[tuple[str, str], ServiceEdge] = {}

    # Strategy 1: Manual (confidence 1.0).
    for edge in (manual_edges or []):
        _put_edge(edges, _with_defaults(edge, strategy="manual", confidence=1.0))

    # Strategy 2: Trace call graph (confidence 0.8-0.95).
    for edge in (trace_edges or []):
        _put_edge(edges, _with_defaults(edge, strategy="trace", confidence=0.85))

    # Strategy 3: Env-var based (confidence 0.5-0.7).
    for wl in k8s_result.workloads:
        for target in wl.env_service_refs:
            if target == wl.name:
                continue
            _put_edge(edges, ServiceEdge(
                source_service=wl.name,
                target_service=target,
                confidence=0.6,
                strategy="env",
                evidence={"source_workload": wl.name, "env_service_ref": target},
            ))
    for ref in (env_references or []):
        _put_edge(edges, ServiceEdge(
            source_service=ref.source_service,
            target_service=ref.target_service,
            confidence=0.6,
            strategy="env",
            evidence=ref.evidence,
        ))

    # Strategy 4: Configmap-based (confidence 0.4-0.7).
    config_maps = {
        (cm.namespace, cm.name): cm
        for cm in getattr(k8s_result, "config_maps", [])
    }
    for wl in k8s_result.workloads:
        for config_map_name in wl.config_map_refs:
            config_map = config_maps.get((wl.namespace, config_map_name))
            if config_map is None:
                continue
            for service_ref in config_map.service_refs:
                target = service_ref.get("target_service")
                if not target or target == wl.name:
                    continue
                _put_edge(edges, ServiceEdge(
                    source_service=wl.name,
                    target_service=target,
                    confidence=0.5,
                    strategy="configmap",
                    evidence={
                        "source_workload": wl.name,
                        "configmap": config_map.name,
                        "key": service_ref.get("key", ""),
                        "config_service_ref": target,
                    },
                ))
    for ref in (configmap_references or []):
        _put_edge(edges, ServiceEdge(
            source_service=ref.source_service,
            target_service=ref.target_service,
            confidence=0.5,
            strategy="configmap",
            evidence=ref.evidence,
        ))

    return sorted(edges.values(), key=lambda e: -e.confidence)


def _workload_from_owner_refs(
    owner_refs: list[dict[str, Any]],
    workloads_by_key: dict[tuple[str, str, str], Any],
    namespace: str,
) -> Any | None:
    for ref in owner_refs:
        kind = ref.get("kind")
        name = ref.get("name")
        if not kind or not name:
            continue
        direct = workloads_by_key.get((namespace, kind, name))
        if direct is not None:
            return direct
        if kind == "ReplicaSet":
            deployment = _deployment_from_replicaset_name(str(name))
            if deployment:
                indirect = workloads_by_key.get((namespace, "Deployment", deployment))
                if indirect is not None:
                    return indirect
    return None


def _deployment_from_replicaset_name(name: str) -> str | None:
    parts = name.rsplit("-", 1)
    if len(parts) != 2:
        return None
    return parts[0]


def _put_edge(
    edges: dict[tuple[str, str], ServiceEdge],
    edge: ServiceEdge,
) -> None:
    key = (edge.source_service, edge.target_service)
    existing = edges.get(key)
    if existing is None or edge.confidence > existing.confidence:
        edges[key] = edge


def _with_defaults(
    edge: ServiceEdge,
    *,
    strategy: Literal["manual", "trace", "env", "configmap"],
    confidence: float,
) -> ServiceEdge:
    return ServiceEdge(
        source_service=edge.source_service,
        target_service=edge.target_service,
        protocol=edge.protocol,
        confidence=max(edge.confidence, confidence),
        strategy=strategy,
        evidence=edge.evidence,
    )


def _selector_matches(
    selector: dict[str, str],
    labels: dict[str, str],
) -> bool:
    """Check if labels satisfy a K8s label selector (equality-only)."""
    if not selector:
        return False
    for key, val in selector.items():
        if labels.get(key) != val:
            return False
    return True
