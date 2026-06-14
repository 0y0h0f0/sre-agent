"""Unit tests for remediation executor backends."""

from __future__ import annotations

from packages.tools.executor_backends import (
    _LIVE_HANDLERS,
    _LIVE_ROLLBACK_HANDLERS,
    ROLLBACK_ACTION_TYPES,
    ExecutionContext,
    ExecutionResult,
    LiveK8sExecutorBackend,
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


def test_live_executor_rollback_fills_params_from_snapshot(monkeypatch) -> None:
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
    assert captured["params"]["to_revision"] == "5"


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
