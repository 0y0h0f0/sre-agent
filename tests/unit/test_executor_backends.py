"""Unit tests for remediation executor backends."""

from __future__ import annotations

import logging
import sys
import types

from packages.common.settings import Settings
from packages.tools.executor_backends import (
    _LIVE_HANDLERS,
    _LIVE_ROLLBACK_HANDLERS,
    ROLLBACK_ACTION_TYPES,
    ExecutionContext,
    ExecutionResult,
    LiveK8sExecutorBackend,
    _ensure_k8s_client,
    _live_pause_rollout,
    _live_restart_statefulset,
    _live_resume_rollout,
    _live_rollback_release,
    _live_scale_deployment,
    build_executor_backend,
    coerce_live_rollback_revision,
)


def test_live_executor_uses_backend_namespace_when_context_namespace_missing(monkeypatch) -> None:
    captured = {}

    def handler(atype, target, params, ns, timeout):
        captured["ns"] = ns
        return ExecutionResult(status="succeeded", message="ok")

    monkeypatch.setitem(_LIVE_HANDLERS, "restart_pod", handler)

    backend = LiveK8sExecutorBackend(namespace="payments")
    result = backend.execute(
        {"type": "restart_pod", "target": "checkout", "params": {}},
        ExecutionContext(service="checkout", incident_id="inc", agent_run_id="run"),
    )

    assert result.status == "succeeded"
    assert captured["ns"] == "payments"


def test_build_executor_backend_defaults_to_fixture() -> None:
    assert build_executor_backend(Settings()).name == "fixture"


def test_build_executor_backend_normalizes_backend_name() -> None:
    assert build_executor_backend(Settings(executor_backend=" fixture ")).name == "fixture"
    assert build_executor_backend(Settings(executor_backend=" LIVE ")).name == "live"


def test_build_executor_backend_rejects_unknown_backend() -> None:
    try:
        build_executor_backend(Settings(executor_backend="liv"))
    except ValueError as exc:
        assert "unknown executor_backend 'liv'" in str(exc)
    else:
        raise AssertionError("unknown executor_backend should fail closed")


def test_build_executor_backend_live_uses_k8s_namespace_fallback() -> None:
    backend = build_executor_backend(
        Settings(
            executor_backend="live",
            executor_k8s_namespace="",
            k8s_namespace="payments",
        )
    )

    assert backend.name == "live"
    assert backend.namespace == "payments"


def test_build_executor_backend_live_empty_namespaces_use_default() -> None:
    backend = build_executor_backend(
        Settings(
            executor_backend="live",
            executor_k8s_namespace="",
            k8s_namespace="",
        )
    )

    assert backend.name == "live"
    assert backend.namespace == "default"


def test_live_executor_defaults_empty_namespace_to_default(monkeypatch) -> None:
    captured = {}

    def handler(atype, target, params, ns, timeout):
        captured["ns"] = ns
        return ExecutionResult(status="succeeded", message="ok")

    monkeypatch.setitem(_LIVE_HANDLERS, "restart_pod", handler)

    backend = LiveK8sExecutorBackend(namespace="")
    result = backend.execute(
        {"type": "restart_pod", "target": "checkout", "params": {}},
        ExecutionContext(service="checkout", incident_id="inc", agent_run_id="run"),
    )

    assert result.status == "succeeded"
    assert captured["ns"] == "default"


def test_live_executor_normalizes_namespace_and_target(monkeypatch) -> None:
    captured = {}

    def handler(atype, target, params, ns, timeout):
        captured["target"] = target
        captured["ns"] = ns
        return ExecutionResult(status="succeeded", message="ok")

    monkeypatch.setitem(_LIVE_HANDLERS, "restart_pod", handler)

    backend = LiveK8sExecutorBackend(namespace=" fallback ")
    result = backend.execute(
        {"type": "restart_pod", "target": " checkout ", "params": {}},
        ExecutionContext(
            service="checkout",
            incident_id="inc",
            agent_run_id="run",
            namespace=" payments ",
        ),
    )

    assert result.status == "succeeded"
    assert backend.namespace == "fallback"
    assert captured == {"target": "checkout", "ns": "payments"}


def test_live_executor_uses_backend_namespace_when_context_namespace_is_blank(
    monkeypatch,
) -> None:
    captured = {}

    def handler(atype, target, params, ns, timeout):
        captured["ns"] = ns
        return ExecutionResult(status="succeeded", message="ok")

    monkeypatch.setitem(_LIVE_HANDLERS, "restart_pod", handler)

    backend = LiveK8sExecutorBackend(namespace=" payments ")
    result = backend.execute(
        {"type": "restart_pod", "target": "checkout", "params": {}},
        ExecutionContext(
            service="checkout",
            incident_id="inc",
            agent_run_id="run",
            namespace="   ",
        ),
    )

    assert result.status == "succeeded"
    assert captured["ns"] == "payments"


def test_live_executor_rejects_empty_target() -> None:
    backend = LiveK8sExecutorBackend(namespace="payments")

    result = backend.execute(
        {"type": "restart_pod", "target": "", "params": {}},
        ExecutionContext(service="checkout", incident_id="inc", agent_run_id="run"),
    )

    assert result.status == "failed"
    assert "invalid k8s resource name for target" in result.message


def test_live_executor_rejects_non_object_params() -> None:
    backend = LiveK8sExecutorBackend(namespace="payments")

    result = backend.execute(
        {"type": "restart_pod", "target": "checkout", "params": ["force"]},
        ExecutionContext(service="checkout", incident_id="inc", agent_run_id="run"),
    )

    assert result.status == "failed"
    assert result.message == "live action params must be an object"


def test_live_executor_rejects_unexpected_params_before_k8s_call(monkeypatch) -> None:
    def handler(atype, target, params, ns, timeout):
        raise AssertionError("handler should not be called when params fail")

    monkeypatch.setitem(_LIVE_HANDLERS, "pause_rollout", handler)

    backend = LiveK8sExecutorBackend(namespace="payments")
    result = backend.execute(
        {"type": "pause_rollout", "target": "checkout", "params": {"force": True}},
        ExecutionContext(service="checkout", incident_id="inc", agent_run_id="run"),
    )

    assert result.status == "failed"
    assert result.message == "live action 'pause_rollout' received unsupported params"
    assert result.details == {
        "allowed_params": [],
        "unexpected_params": ["force"],
    }


def test_live_executor_rejects_extra_scale_params_before_k8s_call(monkeypatch) -> None:
    def handler(atype, target, params, ns, timeout):
        raise AssertionError("handler should not be called when params fail")

    monkeypatch.setitem(_LIVE_HANDLERS, "scale_deployment", handler)

    backend = LiveK8sExecutorBackend(namespace="payments")
    result = backend.execute(
        {
            "type": "scale_deployment",
            "target": "checkout",
            "params": {"replicas": 4, "memory": "1Gi"},
        },
        ExecutionContext(service="checkout", incident_id="inc", agent_run_id="run"),
    )

    assert result.status == "failed"
    assert result.message == "live action 'scale_deployment' received unsupported params"
    assert result.details == {
        "allowed_params": ["replicas"],
        "unexpected_params": ["memory"],
    }


def test_live_executor_redacts_execution_exception_logs(monkeypatch, caplog) -> None:
    def handler(atype, target, params, ns, timeout):
        raise RuntimeError("k8s failed token=short-secret")

    monkeypatch.setitem(_LIVE_HANDLERS, "restart_pod", handler)

    backend = LiveK8sExecutorBackend(namespace="payments")
    with caplog.at_level(logging.ERROR, logger="packages.tools.executor_backends"):
        result = backend.execute(
            {"type": "restart_pod", "target": "checkout", "params": {}},
            ExecutionContext(service="checkout", incident_id="inc", agent_run_id="run"),
        )

    log_text = caplog.text
    assert result.status == "failed"
    assert result.details == {"error_type": "RuntimeError"}
    assert "[REDACTED]" in log_text
    assert "short-secret" not in log_text
    assert "short-secret" not in str(result.model_dump())


def test_live_executor_scale_back_fills_only_replicas_from_snapshot(monkeypatch) -> None:
    captured = {}

    def handler(atype, target, params, ns, timeout):
        captured["params"] = dict(params)
        captured["ns"] = ns
        return ExecutionResult(status="succeeded", message="rollback")

    monkeypatch.setitem(_LIVE_ROLLBACK_HANDLERS, "scale_back", handler)

    backend = LiveK8sExecutorBackend(namespace="payments")
    result = backend.rollback(
        {"type": "scale_back", "target": "checkout", "params": {}},
        {"k8s": {"revision": "5", "replicas": 2}},
        ExecutionContext(service="checkout", incident_id="inc", agent_run_id="run"),
    )

    assert result.status == "succeeded"
    assert captured["ns"] == "payments"
    assert captured["params"]["replicas"] == 2
    assert "to_revision" not in captured["params"]


def test_live_executor_rollback_normalizes_namespace_and_target(monkeypatch) -> None:
    captured = {}

    def handler(atype, target, params, ns, timeout):
        captured["target"] = target
        captured["ns"] = ns
        return ExecutionResult(status="succeeded", message="rollback")

    monkeypatch.setitem(_LIVE_ROLLBACK_HANDLERS, "scale_back", handler)

    backend = LiveK8sExecutorBackend(namespace=" fallback ")
    result = backend.rollback(
        {"type": "scale_back", "target": " checkout ", "params": {"replicas": 2}},
        {"k8s": {"revision": "5", "replicas": 1}},
        ExecutionContext(
            service="checkout",
            incident_id="inc",
            agent_run_id="run",
            namespace=" payments ",
        ),
    )

    assert result.status == "succeeded"
    assert captured == {"target": "checkout", "ns": "payments"}


def test_live_executor_redacts_rollback_exception_logs(monkeypatch, caplog) -> None:
    def handler(atype, target, params, ns, timeout):
        raise RuntimeError("rollback failed token=short-secret")

    monkeypatch.setitem(_LIVE_ROLLBACK_HANDLERS, "scale_back", handler)

    backend = LiveK8sExecutorBackend(namespace="payments")
    with caplog.at_level(logging.ERROR, logger="packages.tools.executor_backends"):
        result = backend.rollback(
            {"type": "scale_back", "target": "checkout", "params": {"replicas": 2}},
            {"k8s": {"replicas": 1}},
            ExecutionContext(service="checkout", incident_id="inc", agent_run_id="run"),
        )

    log_text = caplog.text
    assert result.status == "failed"
    assert result.details == {"error_type": "RuntimeError"}
    assert "[REDACTED]" in log_text
    assert "short-secret" not in log_text
    assert "short-secret" not in str(result.model_dump())


def test_live_executor_redacts_k8s_config_load_failures(monkeypatch) -> None:
    class _Config:
        class ConfigException(Exception):
            pass

        @staticmethod
        def load_incluster_config() -> None:
            raise RuntimeError("incluster token=short-secret")

        @staticmethod
        def load_kube_config() -> None:
            raise RuntimeError("kubeconfig token=other-secret")

    fake_kubernetes = types.ModuleType("kubernetes")
    fake_kubernetes.config = _Config  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "kubernetes", fake_kubernetes)

    try:
        _ensure_k8s_client()
    except RuntimeError as exc:
        message = str(exc)
    else:
        raise AssertionError("k8s config loading should fail")

    assert "[REDACTED]" in message
    assert "short-secret" not in message
    assert "other-secret" not in message


def test_live_executor_rollback_release_fills_only_revision_from_snapshot(
    monkeypatch,
) -> None:
    captured = {}

    def handler(atype, target, params, ns, timeout):
        captured["params"] = dict(params)
        return ExecutionResult(status="succeeded", message="rollback")

    monkeypatch.setitem(_LIVE_ROLLBACK_HANDLERS, "rollback_release", handler)

    backend = LiveK8sExecutorBackend(namespace="payments")
    result = backend.rollback(
        {"type": "rollback_release", "target": "checkout", "params": {}},
        {"k8s": {"revision": "5", "replicas": 2}},
        ExecutionContext(service="checkout", incident_id="inc", agent_run_id="run"),
    )

    assert result.status == "succeeded"
    assert captured["params"]["to_revision"] == "5"
    assert "replicas" not in captured["params"]


def test_live_scale_rejects_missing_or_unsafe_replica_counts() -> None:
    backend = LiveK8sExecutorBackend(namespace="payments")

    unsafe_values = [None, True, -1, "2.5", "many", 51]
    for replicas in unsafe_values:
        params = {} if replicas is None else {"replicas": replicas}
        result = backend.execute(
            {"type": "scale_deployment", "target": "checkout", "params": params},
            ExecutionContext(service="checkout", incident_id="inc", agent_run_id="run"),
        )

        assert result.status == "failed", f"should reject replicas={replicas!r}"
        assert "replicas" in result.message
        assert result.details == {"min_replicas": 0, "max_replicas": 50}


def test_live_scale_records_scale_subresource_details(monkeypatch) -> None:
    captured = {}

    class AppsV1Api:
        def patch_namespaced_deployment_scale(self, *, name, namespace, body, _request_timeout):
            captured["name"] = name
            captured["namespace"] = namespace
            captured["body"] = body
            captured["timeout"] = _request_timeout

    fake_kubernetes = types.ModuleType("kubernetes")
    fake_kubernetes.client = types.SimpleNamespace(AppsV1Api=lambda: AppsV1Api())  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "kubernetes", fake_kubernetes)
    monkeypatch.setattr("packages.tools.executor_backends._ensure_k8s_client", lambda: None)

    backend = LiveK8sExecutorBackend(namespace="payments", timeout_seconds=12.5)
    result = backend.execute(
        {"type": "scale_deployment", "target": "checkout", "params": {"replicas": "4"}},
        ExecutionContext(service="checkout", incident_id="inc", agent_run_id="run"),
    )

    assert result.status == "succeeded"
    assert captured == {
        "name": "checkout",
        "namespace": "payments",
        "body": {"spec": {"replicas": 4}},
        "timeout": 12.5,
    }
    assert result.details == {
        "resource": "deployment",
        "target": "checkout",
        "namespace": "payments",
        "subresource": "scale",
        "replicas": 4,
    }


def test_live_handler_preflight_rejects_invalid_target_before_k8s_client(
    monkeypatch,
) -> None:
    def _fail_if_called() -> None:
        raise AssertionError("k8s client should not be initialized")

    monkeypatch.setattr("packages.tools.executor_backends._ensure_k8s_client", _fail_if_called)

    result = _live_pause_rollout("pause_rollout", "../checkout", {}, "payments", 12.5)

    assert result.status == "failed"
    assert result.message == "invalid k8s resource name for target: ../checkout"


def test_live_handler_preflight_rejects_unexpected_params_before_k8s_client(
    monkeypatch,
) -> None:
    def _fail_if_called() -> None:
        raise AssertionError("k8s client should not be initialized")

    monkeypatch.setattr("packages.tools.executor_backends._ensure_k8s_client", _fail_if_called)

    result = _live_pause_rollout("pause_rollout", "checkout", {"force": True}, "payments", 12.5)

    assert result.status == "failed"
    assert result.message == "live action 'pause_rollout' received unsupported params"
    assert result.details == {
        "allowed_params": [],
        "unexpected_params": ["force"],
    }


def test_live_handler_preflight_canonicalizes_action_alias_params(
    monkeypatch,
) -> None:
    def _fail_if_called() -> None:
        raise AssertionError("k8s client should not be initialized")

    monkeypatch.setattr("packages.tools.executor_backends._ensure_k8s_client", _fail_if_called)

    result = _live_rollback_release(
        "rollback_deployment",
        "checkout",
        {"to_revision": "7", "force": True},
        "payments",
        12.5,
    )

    assert result.status == "failed"
    assert result.message == "live action 'rollback_release' received unsupported params"
    assert result.details == {
        "allowed_params": ["to_revision"],
        "unexpected_params": ["force"],
    }


def test_live_handler_preflight_normalizes_target_and_namespace(monkeypatch) -> None:
    captured = {}

    class AppsV1Api:
        def patch_namespaced_deployment_scale(self, *, name, namespace, body, _request_timeout):
            captured["name"] = name
            captured["namespace"] = namespace
            captured["body"] = body
            captured["timeout"] = _request_timeout

    fake_kubernetes = types.ModuleType("kubernetes")
    fake_kubernetes.client = types.SimpleNamespace(AppsV1Api=lambda: AppsV1Api())  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "kubernetes", fake_kubernetes)
    monkeypatch.setattr("packages.tools.executor_backends._ensure_k8s_client", lambda: None)

    result = _live_scale_deployment(
        "scale_deployment",
        " checkout ",
        {"replicas": "2"},
        " payments ",
        12.5,
    )

    assert result.status == "succeeded"
    assert captured == {
        "name": "checkout",
        "namespace": "payments",
        "body": {"spec": {"replicas": 2}},
        "timeout": 12.5,
    }
    assert result.details["target"] == "checkout"
    assert result.details["namespace"] == "payments"


def test_live_rollback_revision_coercion_requires_positive_integer() -> None:
    assert coerce_live_rollback_revision(None) == (None, None)
    assert coerce_live_rollback_revision(7) == (7, None)
    assert coerce_live_rollback_revision(" 7 ") == (7, None)

    for value in (True, 0, -1, "0", "1.5", "latest"):
        revision, error_message = coerce_live_rollback_revision(value)
        assert revision is None
        assert error_message == "rollback_release to_revision must be a positive integer"


def test_live_rollback_release_passes_integer_revision_to_subresource(monkeypatch) -> None:
    captured = {}

    class _ApiClient:
        def call_api(self, path, method, body, auth_settings, _request_timeout):
            captured["path"] = path
            captured["method"] = method
            captured["body"] = body
            captured["auth_settings"] = auth_settings
            captured["timeout"] = _request_timeout

    class AppsV1Api:
        api_client = _ApiClient()

    fake_kubernetes = types.ModuleType("kubernetes")
    fake_kubernetes.client = types.SimpleNamespace(AppsV1Api=lambda: AppsV1Api())  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "kubernetes", fake_kubernetes)
    monkeypatch.setattr("packages.tools.executor_backends._ensure_k8s_client", lambda: None)

    result = _live_rollback_release(
        "rollback_release",
        "checkout",
        {"to_revision": "7"},
        "payments",
        12.5,
    )

    assert result.status == "succeeded"
    assert captured == {
        "path": "/apis/apps/v1/namespaces/payments/deployments/checkout/rollback",
        "method": "POST",
        "body": {"name": "checkout", "rollbackTo": {"revision": 7}},
        "auth_settings": ["BearerToken"],
        "timeout": 12.5,
    }
    assert result.details == {
        "resource": "deployment",
        "target": "checkout",
        "namespace": "payments",
        "subresource": "rollback",
        "to_revision": 7,
    }


def test_live_pause_rollout_patches_only_paused_true(monkeypatch) -> None:
    captured = {}

    class AppsV1Api:
        def patch_namespaced_deployment(self, *, name, namespace, body, _request_timeout):
            captured["name"] = name
            captured["namespace"] = namespace
            captured["body"] = body
            captured["timeout"] = _request_timeout

    fake_kubernetes = types.ModuleType("kubernetes")
    fake_kubernetes.client = types.SimpleNamespace(AppsV1Api=lambda: AppsV1Api())  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "kubernetes", fake_kubernetes)
    monkeypatch.setattr("packages.tools.executor_backends._ensure_k8s_client", lambda: None)

    result = _live_pause_rollout("pause_rollout", "checkout", {}, "payments", 12.5)

    assert result.status == "succeeded"
    assert result.details == {
        "resource": "deployment",
        "target": "checkout",
        "namespace": "payments",
        "patch": "spec.paused",
        "paused": True,
    }
    assert captured == {
        "name": "checkout",
        "namespace": "payments",
        "body": {"spec": {"paused": True}},
        "timeout": 12.5,
    }


def test_live_resume_rollout_patches_only_paused_false(monkeypatch) -> None:
    captured = {}

    class AppsV1Api:
        def patch_namespaced_deployment(self, *, name, namespace, body, _request_timeout):
            captured["name"] = name
            captured["namespace"] = namespace
            captured["body"] = body
            captured["timeout"] = _request_timeout

    fake_kubernetes = types.ModuleType("kubernetes")
    fake_kubernetes.client = types.SimpleNamespace(AppsV1Api=lambda: AppsV1Api())  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "kubernetes", fake_kubernetes)
    monkeypatch.setattr("packages.tools.executor_backends._ensure_k8s_client", lambda: None)

    result = _live_resume_rollout("resume_rollout", "checkout", {}, "payments", 12.5)

    assert result.status == "succeeded"
    assert result.details == {
        "resource": "deployment",
        "target": "checkout",
        "namespace": "payments",
        "patch": "spec.paused",
        "paused": False,
    }
    assert captured == {
        "name": "checkout",
        "namespace": "payments",
        "body": {"spec": {"paused": False}},
        "timeout": 12.5,
    }


def test_live_restart_deployment_patches_only_template_annotation(monkeypatch) -> None:
    captured = {}

    class AppsV1Api:
        def patch_namespaced_deployment(self, *, name, namespace, body, _request_timeout):
            captured["name"] = name
            captured["namespace"] = namespace
            captured["body"] = body
            captured["timeout"] = _request_timeout

    fake_kubernetes = types.ModuleType("kubernetes")
    fake_kubernetes.client = types.SimpleNamespace(AppsV1Api=lambda: AppsV1Api())  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "kubernetes", fake_kubernetes)
    monkeypatch.setattr("packages.tools.executor_backends._ensure_k8s_client", lambda: None)
    monkeypatch.setattr("packages.tools.executor_backends._now_iso", lambda: "2026-06-17T00:00:00Z")

    backend = LiveK8sExecutorBackend(namespace="payments", timeout_seconds=12.5)
    result = backend.execute(
        {"type": "restart_deployment", "target": "checkout", "params": {}},
        ExecutionContext(service="checkout", incident_id="inc", agent_run_id="run"),
    )

    assert result.status == "succeeded"
    assert result.details == {
        "resource": "deployment",
        "target": "checkout",
        "namespace": "payments",
        "patch": "pod_template_annotation",
        "annotation": "kubectl.kubernetes.io/restartedAt",
    }
    assert captured == {
        "name": "checkout",
        "namespace": "payments",
        "body": {
            "spec": {
                "template": {
                    "metadata": {
                        "annotations": {
                            "kubectl.kubernetes.io/restartedAt": "2026-06-17T00:00:00Z"
                        }
                    }
                }
            }
        },
        "timeout": 12.5,
    }


def test_live_restart_statefulset_patches_only_template_annotation(monkeypatch) -> None:
    captured = {}

    class AppsV1Api:
        def patch_namespaced_stateful_set(self, *, name, namespace, body, _request_timeout):
            captured["name"] = name
            captured["namespace"] = namespace
            captured["body"] = body
            captured["timeout"] = _request_timeout

    fake_kubernetes = types.ModuleType("kubernetes")
    fake_kubernetes.client = types.SimpleNamespace(AppsV1Api=lambda: AppsV1Api())  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "kubernetes", fake_kubernetes)
    monkeypatch.setattr("packages.tools.executor_backends._ensure_k8s_client", lambda: None)
    monkeypatch.setattr("packages.tools.executor_backends._now_iso", lambda: "2026-06-17T00:00:00Z")

    result = _live_restart_statefulset("restart_statefulset", "postgres", {}, "data", 12.5)

    assert result.status == "succeeded"
    assert result.details == {
        "resource": "statefulset",
        "target": "postgres",
        "namespace": "data",
        "patch": "pod_template_annotation",
        "annotation": "kubectl.kubernetes.io/restartedAt",
    }
    assert captured == {
        "name": "postgres",
        "namespace": "data",
        "body": {
            "spec": {
                "template": {
                    "metadata": {
                        "annotations": {
                            "kubectl.kubernetes.io/restartedAt": "2026-06-17T00:00:00Z"
                        }
                    }
                }
            }
        },
        "timeout": 12.5,
    }


def test_rollback_deployment_is_a_rollback_alias_for_execute(monkeypatch) -> None:
    captured = {}

    def handler(atype, target, params, ns, timeout):
        captured["atype"] = atype
        captured["target"] = target
        return ExecutionResult(status="succeeded", message="rollback")

    assert "rollback_deployment" in ROLLBACK_ACTION_TYPES
    monkeypatch.setitem(_LIVE_HANDLERS, "rollback_release", handler)

    backend = LiveK8sExecutorBackend(namespace="payments")
    result = backend.execute(
        {"type": "rollback_deployment", "target": "checkout", "params": {}},
        ExecutionContext(service="checkout", incident_id="inc", agent_run_id="run"),
    )

    assert result.status == "succeeded"
    assert captured == {"atype": "rollback_release", "target": "checkout"}


def test_rollback_deployment_is_a_rollback_alias_for_rollback(monkeypatch) -> None:
    captured = {}

    def handler(atype, target, params, ns, timeout):
        captured["atype"] = atype
        captured["params"] = dict(params)
        return ExecutionResult(status="succeeded", message="rollback")

    monkeypatch.setitem(_LIVE_ROLLBACK_HANDLERS, "rollback_release", handler)

    backend = LiveK8sExecutorBackend(namespace="payments")
    result = backend.rollback(
        {"type": "rollback_deployment", "target": "checkout", "params": {}},
        {"k8s": {"revision": "5"}},
        ExecutionContext(service="checkout", incident_id="inc", agent_run_id="run"),
    )

    assert result.status == "succeeded"
    assert captured["atype"] == "rollback_release"
    assert captured["params"]["to_revision"] == "5"


def test_k8s_name_validation_rejects_invalid_target() -> None:
    """K8s resource names with invalid characters are rejected."""
    from packages.tools.executor_backends import (
        ExecutionContext,
        LiveK8sExecutorBackend,
    )

    backend = LiveK8sExecutorBackend(namespace="default")
    invalid_names = [
        "_leading_underscore",
        "-leading-hyphen",
        "UppercaseName",
        "name@with!special",
        "a" * 64,  # too long
        "name..with..dots",
    ]
    for name in invalid_names:
        result = backend.execute(
            {"type": "restart_pod", "target": name, "params": {}},
            ExecutionContext(service="test", incident_id="inc", agent_run_id="run"),
        )
        assert result.status == "failed", f"should reject: {name}"
        assert "invalid k8s resource name" in result.message


def test_k8s_name_validation_allows_valid_names(monkeypatch) -> None:
    """Valid K8s DNS-1123 label names pass validation."""
    from packages.tools.executor_backends import (
        _LIVE_HANDLERS,
        ExecutionContext,
        ExecutionResult,
        LiveK8sExecutorBackend,
    )

    def _fake_handler(atype, target, params, ns, timeout):
        return ExecutionResult(status="succeeded", message="ok")

    monkeypatch.setitem(_LIVE_HANDLERS, "restart_pod", _fake_handler)

    backend = LiveK8sExecutorBackend(namespace="default")
    valid_names = ["checkout", "api-gateway", "a", "my-service-v2", "abc123"]
    for name in valid_names:
        result = backend.execute(
            {"type": "restart_pod", "target": name, "params": {}},
            ExecutionContext(service="test", incident_id="inc", agent_run_id="run"),
        )
        assert result.status == "succeeded", f"should accept: {name}"


def test_k8s_name_validation_rejects_invalid_namespace() -> None:
    """Invalid namespace names are rejected."""
    from packages.tools.executor_backends import (
        ExecutionContext,
        LiveK8sExecutorBackend,
    )

    backend = LiveK8sExecutorBackend(namespace="default")
    result = backend.execute(
        {"type": "restart_pod", "target": "checkout", "params": {}},
        ExecutionContext(
            service="test",
            incident_id="inc",
            agent_run_id="run",
            namespace="_invalid_ns",
        ),
    )
    assert result.status == "failed"
    assert "invalid k8s resource name for namespace" in result.message
