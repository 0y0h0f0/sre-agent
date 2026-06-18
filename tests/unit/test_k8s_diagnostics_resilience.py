"""Regression coverage for live K8s diagnostics degradation paths."""

from __future__ import annotations

import sys
import types
from types import SimpleNamespace
from typing import Any

from packages.agent.nodes.collect_k8s import collect_k8s
from packages.common.settings import Settings
from packages.tools.k8s import K8sDiagnosticsTool, K8sQuery, LiveK8sBackend


def test_live_k8s_config_failure_degrades_with_both_errors(monkeypatch) -> None:
    calls: list[str] = []

    def _raise_incluster() -> None:
        calls.append("incluster")
        raise RuntimeError("service host missing")

    def _raise_kubeconfig() -> None:
        calls.append("kubeconfig")
        raise RuntimeError("No configuration found")

    fake_kubernetes = types.ModuleType("kubernetes")
    fake_kubernetes.client = types.SimpleNamespace(CoreV1Api=lambda: object())  # type: ignore[attr-defined]
    fake_kubernetes.config = types.SimpleNamespace(  # type: ignore[attr-defined]
        load_incluster_config=_raise_incluster,
        load_kube_config=_raise_kubeconfig,
    )
    monkeypatch.setitem(sys.modules, "kubernetes", fake_kubernetes)

    tool = K8sDiagnosticsTool(backend=LiveK8sBackend(namespace="target-namespace"))
    result = tool.run(
        K8sQuery(service="task-service", operation="events", namespace="target-namespace")
    )

    assert result.status == "degraded"
    assert calls == ["incluster", "kubeconfig"]
    assert result.error_message is not None
    assert "in-cluster config failed: service host missing" in result.error_message
    assert "kubeconfig failed: No configuration found" in result.error_message


def test_collect_k8s_records_degraded_tool_call_when_backend_raises() -> None:
    class _BrokenK8sTool:
        name = "k8s"
        timeout_seconds = 1.0

        def run(self, query: K8sQuery) -> Any:
            raise RuntimeError("backend exploded")

    tool_calls: list[dict[str, Any]] = []
    node_traces: list[dict[str, Any]] = []
    deps = SimpleNamespace(
        settings=Settings(
            database_url="sqlite://",
            redis_url="memory://",
            celery_broker_url="memory://",
            celery_result_backend="memory://",
            k8s_namespace="target-namespace",
        ),
        k8s_tool=_BrokenK8sTool(),
        tool_call_recorder=lambda **kwargs: tool_calls.append(kwargs),
        node_tracer=lambda **kwargs: node_traces.append(kwargs),
    )
    state = {
        "incident_id": "inc_k8s",
        "agent_run_id": "run_k8s",
        "alert_name": "PodRestartLoop",
        "service_name": "task-service",
        "severity": "P2",
        "errors": [],
    }

    result = collect_k8s(state, deps)  # type: ignore[arg-type]

    assert result["phase"] == "k8s_collected"
    assert result["k8s_evidence"][0]["status"] == "degraded"
    assert result["errors"] == [{"node": "collect_k8s", "error": "backend exploded"}]
    assert tool_calls[0]["tool_name"] == "k8s"
    assert tool_calls[0]["result"].status == "degraded"
    assert tool_calls[0]["result"].error_message == "backend exploded"
    assert node_traces[0]["status"] == "degraded"
