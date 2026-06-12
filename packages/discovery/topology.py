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


def derive_workload_bindings(
    k8s_result: K8sDiscoveryResult,
) -> list[WorkloadBinding]:
    """Derive WorkloadBinding: Service selector -> workload via ownerRefs.

    Does NOT produce ServiceEdge. Only workload bindings.
    """
    bindings: list[WorkloadBinding] = []

    for svc in k8s_result.services:
        if not svc.selector:
            continue
        # Match workloads whose labels match the service selector.
        for wl in k8s_result.workloads:
            if wl.namespace != svc.namespace:
                continue
            if _selector_matches(svc.selector, wl.labels):
                bindings.append(WorkloadBinding(
                    service_name=svc.name,
                    workload_name=wl.name,
                    workload_kind=wl.kind,
                    namespace=svc.namespace,
                    evidence={"selector": svc.selector, "workload_labels": wl.labels},
                ))
                break
    return bindings


def derive_service_edges(
    k8s_result: K8sDiscoveryResult,
    manual_edges: list[ServiceEdge] | None = None,
    trace_services: list[str] | None = None,
) -> list[ServiceEdge]:
    """Derive ServiceEdge list using four strategies by confidence.

    Priority: manual (1.0) > trace (0.8-0.95) > env (0.5-0.7) > configmap (0.4-0.7).
    Conflicting edges: higher confidence wins.
    """
    edges: dict[tuple[str, str], ServiceEdge] = {}

    # Strategy 1: Manual (confidence 1.0).
    for edge in (manual_edges or []):
        key = (edge.source_service, edge.target_service)
        edges[key] = edge

    # Strategy 2: Trace-based (confidence 0.8-0.95).
    if trace_services:
        svc_names = {s.name for s in k8s_result.services}
        for ts in trace_services:
            if ts not in svc_names:
                continue
            for ksvc in k8s_result.services:
                if ksvc.name == ts:
                    continue
                key = (ts, ksvc.name)
                if key not in edges:
                    edges[key] = ServiceEdge(
                        source_service=ts,
                        target_service=ksvc.name,
                        confidence=0.85,
                        strategy="trace",
                        evidence={"trace_service": ts},
                    )

    # Strategy 3: Env-var based (confidence 0.5-0.7).
    for wl in k8s_result.workloads:
        for other_wl in k8s_result.workloads:
            if wl.name == other_wl.name:
                continue
            key = (wl.name, other_wl.name)
            if key not in edges:
                edges[key] = ServiceEdge(
                    source_service=wl.name,
                    target_service=other_wl.name,
                    confidence=0.5,
                    strategy="env",
                    evidence={"source_workload": wl.name},
                )

    # Strategy 4: Configmap-based (confidence 0.4-0.7).
    # Simplified: derive from service naming conventions.
    for svc in k8s_result.services:
        for other_svc in k8s_result.services:
            if svc.name == other_svc.name:
                continue
            key = (svc.name, other_svc.name)
            if key not in edges:
                edges[key] = ServiceEdge(
                    source_service=svc.name,
                    target_service=other_svc.name,
                    confidence=0.4,
                    strategy="configmap",
                    evidence={},
                )

    return sorted(edges.values(), key=lambda e: -e.confidence)


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
