"""Tests for M2 PR 2.4 + 2.5: WorkloadBinding and ServiceEdge."""
from __future__ import annotations

from packages.discovery.k8s_discovery import (
    K8sDiscoveryResult,
    K8sService,
    K8sWorkload,
)
from packages.discovery.topology import (
    ServiceEdge,
    WorkloadBinding,
    derive_service_edges,
    derive_workload_bindings,
)


def _make_result(services=None, workloads=None):
    return K8sDiscoveryResult(
        services=services or [],
        workloads=workloads or [],
        namespaces=["default"],
    )


class TestWorkloadBinding:
    def test_service_selector_to_deployment(self):
        svc = K8sService(name="checkout", namespace="default", selector={"app": "checkout"})
        wl = K8sWorkload(name="checkout-deploy", namespace="default", kind="Deployment",
                          labels={"app": "checkout"})
        bindings = derive_workload_bindings(_make_result([svc], [wl]))
        assert len(bindings) == 1
        assert bindings[0].service_name == "checkout"
        assert bindings[0].workload_kind == "Deployment"

    def test_never_creates_service_edge(self):
        svc = K8sService(name="checkout", namespace="default", selector={"app": "checkout"})
        wl = K8sWorkload(name="checkout-deploy", namespace="default", kind="Deployment",
                          labels={"app": "checkout"})
        bindings = derive_workload_bindings(_make_result([svc], [wl]))
        for b in bindings:
            assert isinstance(b, WorkloadBinding)

    def test_missing_selector_no_binding(self):
        svc = K8sService(name="checkout", namespace="default")
        wl = K8sWorkload(name="checkout-deploy", namespace="default", kind="Deployment")
        bindings = derive_workload_bindings(_make_result([svc], [wl]))
        assert len(bindings) == 0


class TestServiceEdge:
    def test_manual_topology_highest_priority(self):
        manual = [ServiceEdge(source_service="checkout", target_service="payments",
                              strategy="manual", confidence=1.0)]
        edges = derive_service_edges(_make_result(), manual_edges=manual)
        assert edges[0].confidence == 1.0

    def test_edge_has_evidence_field(self):
        svc1 = K8sService(name="checkout", namespace="default")
        svc2 = K8sService(name="payments", namespace="default")
        edges = derive_service_edges(_make_result([svc1, svc2]))
        for e in edges:
            assert hasattr(e, "evidence")
            assert e.strategy in ("manual", "trace", "env", "configmap")

    def test_no_duplicate_keys(self):
        svc1 = K8sService(name="checkout", namespace="default")
        svc2 = K8sService(name="payments", namespace="default")
        edges = derive_service_edges(_make_result([svc1, svc2]))
        keys = [(e.source_service, e.target_service) for e in edges]
        assert len(keys) == len(set(keys))
