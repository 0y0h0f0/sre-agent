"""Tests for M2 PR 2.4 + 2.5: WorkloadBinding and ServiceEdge."""
from __future__ import annotations

from packages.discovery.k8s_discovery import (
    K8sConfigMap,
    K8sDiscoveryResult,
    K8sPod,
    K8sService,
    K8sWorkload,
)
from packages.discovery.topology import (
    ServiceEdge,
    ServiceReference,
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
        pod = K8sPod(
            name="checkout-abc",
            namespace="default",
            labels={"app": "checkout"},
            owner_references=[{"kind": "Deployment", "name": "checkout-deploy"}],
        )
        result = K8sDiscoveryResult(
            services=[svc],
            workloads=[wl],
            pods=[pod],
            namespaces=["default"],
        )
        bindings = derive_workload_bindings(result)
        assert len(bindings) == 1
        assert bindings[0].service_name == "checkout"
        assert bindings[0].workload_kind == "Deployment"
        assert bindings[0].evidence["pod"] == "checkout-abc"

    def test_never_creates_service_edge(self):
        svc = K8sService(name="checkout", namespace="default", selector={"app": "checkout"})
        wl = K8sWorkload(name="checkout-deploy", namespace="default", kind="Deployment",
                          labels={"app": "checkout"})
        pod = K8sPod(
            name="checkout-abc",
            namespace="default",
            labels={"app": "checkout"},
            owner_references=[{"kind": "Deployment", "name": "checkout-deploy"}],
        )
        result = K8sDiscoveryResult(
            services=[svc],
            workloads=[wl],
            pods=[pod],
            namespaces=["default"],
        )
        bindings = derive_workload_bindings(result)
        for b in bindings:
            assert isinstance(b, WorkloadBinding)

    def test_missing_owner_ref_no_binding(self):
        svc = K8sService(name="checkout", namespace="default", selector={"app": "checkout"})
        wl = K8sWorkload(name="checkout-deploy", namespace="default", kind="Deployment")
        pod = K8sPod(name="checkout-abc", namespace="default", labels={"app": "checkout"})
        result = K8sDiscoveryResult(
            services=[svc],
            workloads=[wl],
            pods=[pod],
            namespaces=["default"],
        )
        bindings = derive_workload_bindings(result)
        assert len(bindings) == 0

    def test_replicaset_owner_ref_maps_to_deployment(self):
        svc = K8sService(name="checkout", namespace="default", selector={"app": "checkout"})
        wl = K8sWorkload(name="checkout", namespace="default", kind="Deployment")
        pod = K8sPod(
            name="checkout-abc",
            namespace="default",
            labels={"app": "checkout"},
            owner_references=[{"kind": "ReplicaSet", "name": "checkout-7d9c4"}],
        )
        bindings = derive_workload_bindings(K8sDiscoveryResult(
            services=[svc],
            workloads=[wl],
            pods=[pod],
            namespaces=["default"],
        ))
        assert bindings[0].workload_name == "checkout"


class TestServiceEdge:
    def test_manual_topology_highest_priority(self):
        manual = [ServiceEdge(source_service="checkout", target_service="payments",
                              strategy="manual", confidence=1.0)]
        edges = derive_service_edges(_make_result(), manual_edges=manual)
        assert edges[0].confidence == 1.0

    def test_edge_has_evidence_field(self):
        wl = K8sWorkload(
            name="checkout",
            namespace="default",
            kind="Deployment",
            env_service_refs=["payments.default"],
        )
        edges = derive_service_edges(_make_result(workloads=[wl]))
        for e in edges:
            assert hasattr(e, "evidence")
            assert e.strategy in ("manual", "trace", "env", "configmap")

    def test_no_duplicate_keys(self):
        manual = [ServiceEdge(source_service="checkout", target_service="payments",
                              strategy="manual", confidence=1.0)]
        trace = [ServiceEdge(source_service="checkout", target_service="payments",
                             strategy="trace", confidence=0.85)]
        edges = derive_service_edges(_make_result(), manual_edges=manual,
                                     trace_edges=trace)
        keys = [(e.source_service, e.target_service) for e in edges]
        assert len(keys) == len(set(keys))

    def test_trace_call_graph_edge(self):
        trace = [ServiceEdge(source_service="checkout", target_service="payments",
                             strategy="trace", confidence=0.85,
                             evidence={"trace_id": "abc"})]
        edges = derive_service_edges(_make_result(), trace_edges=trace)
        assert edges[0].strategy == "trace"
        assert edges[0].evidence["trace_id"] == "abc"

    def test_env_var_dns_edge(self):
        wl = K8sWorkload(
            name="checkout",
            namespace="default",
            kind="Deployment",
            env_service_refs=["payments.default"],
        )
        edges = derive_service_edges(_make_result(workloads=[wl]))
        assert edges[0].source_service == "checkout"
        assert edges[0].target_service == "payments.default"
        assert edges[0].strategy == "env"

    def test_configmap_edge(self):
        refs = [ServiceReference(
            source_service="checkout",
            target_service="inventory",
            evidence={"configmap": "checkout-config"},
        )]
        edges = derive_service_edges(_make_result(), configmap_references=refs)
        assert edges[0].strategy == "configmap"
        assert edges[0].evidence["configmap"] == "checkout-config"

    def test_configmap_edge_from_k8s_discovery_result(self):
        wl = K8sWorkload(
            name="checkout",
            namespace="default",
            kind="Deployment",
            config_map_refs=["checkout-config"],
        )
        cm = K8sConfigMap(
            name="checkout-config",
            namespace="default",
            service_refs=[
                {"key": "inventory_url", "target_service": "inventory.default"}
            ],
        )
        edges = derive_service_edges(K8sDiscoveryResult(
            workloads=[wl],
            config_maps=[cm],
            namespaces=["default"],
        ))
        assert len(edges) == 1
        assert edges[0].strategy == "configmap"
        assert edges[0].target_service == "inventory.default"
        assert edges[0].evidence == {
            "source_workload": "checkout",
            "configmap": "checkout-config",
            "key": "inventory_url",
            "config_service_ref": "inventory.default",
        }

    def test_conflicting_edges_higher_confidence_wins(self):
        trace = [ServiceEdge(source_service="checkout", target_service="payments",
                             strategy="trace", confidence=0.85)]
        env = [ServiceReference(source_service="checkout", target_service="payments")]
        edges = derive_service_edges(_make_result(), trace_edges=trace,
                                     env_references=env)
        assert len(edges) == 1
        assert edges[0].strategy == "trace"

    def test_no_evidence_returns_empty(self):
        svc1 = K8sService(name="checkout", namespace="default")
        svc2 = K8sService(name="payments", namespace="default")
        assert derive_service_edges(_make_result([svc1, svc2])) == []
