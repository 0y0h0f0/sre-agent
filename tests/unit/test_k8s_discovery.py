"""Tests for M2 PR 2.1: K8sDiscovery."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from packages.discovery.k8s_discovery import (
    K8sConfigMap,
    K8sDiscovery,
    K8sService,
    K8sUnavailableError,
)


def _obj(**kwargs):
    return SimpleNamespace(**kwargs)


def _list(*items):
    return _obj(items=list(items))


def _metadata(name, namespace="prod", labels=None, annotations=None, owners=None):
    return _obj(
        name=name,
        namespace=namespace,
        labels=labels or {},
        annotations=annotations or {},
        owner_references=owners or [],
        uid=f"uid-{name}",
    )


class _FakeCoreV1:
    def list_namespace(self):
        return _list(_obj(metadata=_obj(name="prod")), _obj(metadata=_obj(name="dev")))

    def list_namespaced_service(self, namespace):
        return _list(
            _obj(
                metadata=_metadata("checkout", namespace, labels={"app": "checkout"}),
                spec=_obj(
                    cluster_ip="10.0.0.1",
                    selector={"app": "checkout"},
                    ports=[_obj(name="http", port=8080, protocol="TCP")],
                ),
            )
        )

    def list_namespaced_pod(self, namespace):
        owners = [_obj(kind="Deployment", name="checkout", uid="uid-checkout", controller=True)]
        return _list(
            _obj(
                metadata=_metadata(
                    "checkout-a",
                    namespace,
                    labels={"app": "checkout"},
                    owners=owners,
                )
            ),
            _obj(
                metadata=_metadata(
                    "checkout-b",
                    namespace,
                    labels={"app": "checkout"},
                    owners=owners,
                )
            ),
            _obj(
                metadata=_metadata(
                    "checkout-c",
                    namespace,
                    labels={"app": "checkout"},
                    owners=owners,
                )
            ),
        )

    def list_namespaced_endpoints(self, namespace):
        return _list(
            _obj(
                metadata=_metadata("checkout", namespace),
                subsets=[
                    _obj(
                        addresses=[_obj(ip="10.2.0.10")],
                        ports=[_obj(name="http", port=8080, protocol="TCP")],
                    )
                ],
            )
        )

    def list_namespaced_config_map(self, namespace):
        return _list(
            _obj(
                metadata=_metadata("checkout-config", namespace),
                data={
                    "payments_url": "http://payments.prod.svc.cluster.local",
                    "note": "no service ref here",
                },
            )
        )


class _FakeAppsV1:
    def list_namespaced_deployment(self, namespace):
        template = _obj(
            metadata=_obj(labels={"app": "checkout"}),
            spec=_obj(
                containers=[
                    _obj(
                        env=[
                            _obj(
                                name="PAYMENTS_URL",
                                value="http://payments.prod.svc.cluster.local",
                                value_from=None,
                            ),
                            _obj(
                                name="KUBERNETES_SERVICE_HOST",
                                value="10.0.0.1",
                                value_from=None,
                            ),
                        ],
                        env_from=[],
                    )
                ],
                volumes=[
                    _obj(config_map=_obj(name="checkout-config")),
                ],
            ),
        )
        return _list(
            _obj(
                metadata=_metadata("checkout", namespace, labels={"app": "checkout"}),
                spec=_obj(
                    selector=_obj(match_labels={"app": "checkout"}),
                    replicas=2,
                    template=template,
                ),
                status=_obj(ready_replicas=2),
            )
        )

    def list_namespaced_stateful_set(self, namespace):
        return _list()

    def list_namespaced_daemon_set(self, namespace):
        return _list()


class _FakeNetworkingV1:
    def list_namespaced_ingress(self, namespace):
        return _list(
            _obj(
                metadata=_metadata("checkout-ing", namespace),
                spec=_obj(
                    tls=[_obj(hosts=["checkout.example.com"])],
                    rules=[
                        _obj(
                            host="checkout.example.com",
                            http=_obj(
                                paths=[
                                    _obj(
                                        backend=_obj(
                                            service=_obj(name="checkout")
                                        )
                                    )
                                ]
                            ),
                        )
                    ],
                ),
            )
        )


def _fake_discovery(pod_sample_ratio=1.0):
    discovery = K8sDiscovery(
        namespace_allowlist=["prod"],
        kube_config_file="fake",
        pod_sample_ratio=pod_sample_ratio,
    )
    discovery._client = _obj(
        core_v1=_FakeCoreV1(),
        apps_v1=_FakeAppsV1(),
        networking_v1=_FakeNetworkingV1(),
    )
    return discovery


class TestK8sDiscovery:
    def test_lazy_load_raises_when_kubernetes_missing(self):
        """K8sDiscovery raises K8sUnavailableError when kubernetes not installed."""
        discovery = K8sDiscovery()
        with pytest.raises(K8sUnavailableError):
            _ = discovery._k8s

    def test_discover_all_returns_degraded_when_k8s_unavailable(self):
        discovery = K8sDiscovery()
        result = discovery.discover_all()
        assert result.degraded is True
        assert result.degraded_reason is not None

    def test_namespace_allowlist_filtering(self):
        discovery = K8sDiscovery(namespace_allowlist=["prod", "staging"])
        assert "prod" in discovery._namespace_allowlist
        assert "staging" in discovery._namespace_allowlist

    def test_service_allowlist_defaults_empty(self):
        discovery = K8sDiscovery()
        assert discovery._service_allowlist == []

    def test_discover_deployments_services_pods_endpoints_ingress(self):
        result = _fake_discovery().discover_all()
        assert result.degraded is False
        assert result.namespaces == ["prod"]
        assert result.services[0].name == "checkout"
        assert result.workloads[0].kind == "Deployment"
        assert result.pods[0].owner_references[0]["kind"] == "Deployment"
        assert result.endpoints[0].addresses == ["10.2.0.10"]
        assert result.ingresses[0].service_names == ["checkout"]
        assert isinstance(result.config_maps[0], K8sConfigMap)
        assert result.config_maps[0].service_refs == [
            {"key": "payments_url", "target_service": "payments.prod"}
        ]
        assert result.workloads[0].env_service_refs == ["payments.prod"]
        assert result.workloads[0].config_map_refs == ["checkout-config"]

    def test_service_allowlist_filters_services(self):
        discovery = _fake_discovery()
        discovery._service_allowlist = ["payments"]
        result = discovery.discover_all()
        assert result.services == []

    def test_pod_sample_ratio_is_deterministic(self):
        result = _fake_discovery(pod_sample_ratio=0.34).discover_all()
        assert [pod.name for pod in result.pods] == ["checkout-a", "checkout-b"]

    def test_rbac_forbidden_with_allowlist_degrades_namespace(self):
        class ForbiddenCore(_FakeCoreV1):
            def list_namespace(self):
                raise RuntimeError("403 Forbidden")

            def list_namespaced_service(self, namespace):
                raise RuntimeError("403 Forbidden")

            def list_namespaced_pod(self, namespace):
                raise RuntimeError("403 Forbidden")

            def list_namespaced_endpoints(self, namespace):
                raise RuntimeError("403 Forbidden")

            def list_namespaced_config_map(self, namespace):
                raise RuntimeError("403 Forbidden")

        class ForbiddenApps(_FakeAppsV1):
            def list_namespaced_deployment(self, namespace):
                raise RuntimeError("403 Forbidden")

        discovery = _fake_discovery()
        discovery._client.core_v1 = ForbiddenCore()
        discovery._client.apps_v1 = ForbiddenApps()
        result = discovery.discover_all()
        assert result.degraded is True
        assert any("services" in warning for warning in result.warnings)

    def test_list_services_includes_selector(self):
        result = _fake_discovery().discover_all()
        service = result.services[0]
        assert isinstance(service, K8sService)
        assert service.selector == {"app": "checkout"}

    def test_import_failure_is_cached(self):
        discovery1 = K8sDiscovery()
        discovery2 = K8sDiscovery()
        with pytest.raises(K8sUnavailableError):
            _ = discovery1._k8s
        # Second access should also fail (cached).
        with pytest.raises(K8sUnavailableError):
            _ = discovery2._k8s

    def test_thread_safety_initialization(self):
        """Multiple threads accessing _k8s should be safe."""
        import threading
        discovery = K8sDiscovery()
        errors = []

        def try_access():
            try:
                _ = discovery._k8s
            except K8sUnavailableError:
                errors.append("expected")

        threads = [threading.Thread(target=try_access) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # All should have raised K8sUnavailableError (kubernetes not installed).
        assert len(errors) == 3
