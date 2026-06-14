"""Kubernetes read-only diagnosis tool (roadmap Phase 2.2).

MVP scope is read-only: describe pod, logs, events, rollout status. The fixture
backend keeps tests deterministic and local dev offline; the live backend uses
the kubernetes client against an explicitly configured cluster.

Hard boundary (``00-overview/scope.md``): this tool never performs production
writes. Write-class remediations (cordon/restart/scale/rollout-undo) are only
ever emitted as dry-run *suggestions* via :func:`build_remediation_suggestions`
— they are returned for human approval, never executed here.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, Field, field_validator

from packages.common.settings import Settings
from packages.tools.base import ToolResult, ToolStatus, compact_summary, elapsed_ms, start_timer

# Read-only diagnosis operations. ``operation`` is a free str (not a Literal) so
# the read-only check in K8sDiagnosticsTool.run is the single, testable
# enforcement point — a write-class operation reaches the tool and is refused.
_READ_ONLY_OPERATIONS: frozenset[str] = frozenset(
    {"describe_pod", "logs", "events", "rollout_status", "get_deployment"}
)


class K8sQuery(BaseModel):
    service: str = Field(min_length=1)
    operation: str = "describe_pod"
    namespace: str = "default"
    pod: str | None = None

    @field_validator("service", "namespace", "operation")
    @classmethod
    def _strip(cls, value: str) -> str:
        return value.strip()


class K8sBackend(Protocol):
    name: str

    def fetch(self, query: K8sQuery) -> dict[str, Any]:
        """Return the read-only payload for the requested operation."""


class FixtureK8sBackend:
    """Reads cluster diagnostics from a fixture file (MVP default)."""

    name = "fixture"

    def __init__(self, fixture_path: str | Path = "demo/faults/k8s.json") -> None:
        self.fixture_path = Path(fixture_path)

    def fetch(self, query: K8sQuery) -> dict[str, Any]:
        payload = json.loads(self.fixture_path.read_text(encoding="utf-8"))
        service_block = payload.get(query.service, {})
        if not isinstance(service_block, dict):
            msg = f"k8s fixture for {query.service} must be an object"
            raise ValueError(msg)
        result = service_block.get(query.operation, {})
        return {"operation": query.operation, "payload": result}


class LiveK8sBackend:
    """Read-only diagnostics against a live cluster via the kubernetes client.

    The kubernetes client is imported lazily so the dependency is optional and
    the fixture default never requires it. Only read APIs are used.
    """

    name = "live"

    def __init__(self, *, namespace: str = "default", timeout_seconds: float = 2.0) -> None:
        self.namespace = namespace
        self.timeout_seconds = timeout_seconds

    def fetch(self, query: K8sQuery) -> dict[str, Any]:
        if query.operation not in _READ_ONLY_OPERATIONS:
            msg = f"refusing non-read-only k8s operation '{query.operation}'"
            raise ValueError(msg)
        try:
            from kubernetes import client, config  # type: ignore[import-untyped]
        except ImportError as exc:  # pragma: no cover - optional dependency
            msg = "kubernetes client not installed; use k8s_backend=fixture"
            raise RuntimeError(msg) from exc

        _load_kubernetes_config(config)  # pragma: no cover - requires a real cluster
        core = client.CoreV1Api()  # pragma: no cover
        ns = query.namespace or self.namespace  # pragma: no cover
        timeout = self.timeout_seconds  # pragma: no cover
        if query.operation == "events":  # pragma: no cover
            events = core.list_namespaced_event(ns, _request_timeout=timeout)
            return {
                "operation": "events",
                "payload": [e.message for e in events.items[:50]],
            }
        if query.operation == "logs" and query.pod:  # pragma: no cover
            logs = core.read_namespaced_pod_log(
                query.pod, ns, tail_lines=100, _request_timeout=timeout
            )
            return {"operation": "logs", "payload": logs}
        if query.operation == "describe_pod" and query.pod:  # pragma: no cover
            pod = core.read_namespaced_pod(query.pod, ns, _request_timeout=timeout)
            return {"operation": "describe_pod", "payload": pod.to_dict()}
        if query.operation == "get_deployment":  # pragma: no cover
            apps = client.AppsV1Api()
            try:
                deploy = apps.read_namespaced_deployment(
                    query.service, ns, _request_timeout=timeout
                )
                spec: Any = deploy.spec or {}
                status: Any = deploy.status or {}
                payload = {
                    "name": query.service,
                    "namespace": ns,
                    "replicas": spec.replicas if spec else None,
                    "revision": deploy.metadata.annotations.get(
                        "deployment.kubernetes.io/revision"
                    ) if deploy.metadata and deploy.metadata.annotations else None,
                    "image": (
                        spec.template.spec.containers[0].image
                        if spec and spec.template
                        and spec.template.spec
                        and spec.template.spec.containers
                        else None
                    ),
                    "ready_replicas": status.ready_replicas if status else None,
                    "available_replicas": status.available_replicas if status else None,
                    "conditions": [
                        {"type": c.type, "status": c.status}
                        for c in (status.conditions or [])
                    ] if status else [],
                }
                return {"operation": "get_deployment", "payload": payload}
            except Exception:
                return {"operation": "get_deployment", "payload": {"error": "not_found"}}
        return {"operation": query.operation, "payload": {}}  # pragma: no cover


class K8sDiagnosticsTool:
    name = "k8s"

    def __init__(
        self,
        *,
        backend: K8sBackend | None = None,
        fixture_path: str | None = None,
        timeout_seconds: float = 2.0,
    ) -> None:
        if backend is None:
            backend = FixtureK8sBackend(fixture_path=fixture_path or "demo/faults/k8s.json")
        self.backend = backend
        # Part of the BaseTool contract; the live backend owns the real timeout.
        self.timeout_seconds = timeout_seconds

    def run(self, query: BaseModel) -> ToolResult:
        k8s_query = K8sQuery.model_validate(query)
        started_at = start_timer()
        if k8s_query.operation not in _READ_ONLY_OPERATIONS:
            return ToolResult(
                status="failed",
                data={},
                summary=f"refused non-read-only k8s operation '{k8s_query.operation}'",
                duration_ms=elapsed_ms(started_at),
                error_message="k8s tool is read-only",
            )
        try:
            result_data = self.backend.fetch(k8s_query)
            payload = result_data.get("payload")
            has_data = bool(payload)
            status: ToolStatus = "succeeded" if has_data else "degraded"
            return ToolResult(
                status=status,
                data=result_data,
                summary=compact_summary(
                    {
                        "service": k8s_query.service,
                        "namespace": k8s_query.namespace,
                        "op": k8s_query.operation,
                        "has_data": has_data,
                    }
                ),
                evidence=[
                    {
                        "type": "k8s",
                        "source": self.backend.name,
                        "title": f"{k8s_query.operation} for {k8s_query.service}",
                        "payload": result_data,
                    }
                ]
                if has_data
                else [],
                duration_ms=elapsed_ms(started_at),
                error_message=None if has_data else "empty k8s result",
            )
        except (
            OSError,
            json.JSONDecodeError,
            KeyError,
            TypeError,
            ValueError,
            RuntimeError,
        ) as exc:
            return ToolResult(
                status="degraded",
                data={},
                summary=f"k8s backend unavailable for {k8s_query.service}",
                duration_ms=elapsed_ms(started_at),
                error_message=str(exc),
            )


# Risk levels mirror guardrails-and-approval.md: cordon/restart/scale are L2,
# rollout-undo is L3. These suggestions are dry-run only and never executed.
_WRITE_SUGGESTIONS: dict[str, dict[str, str]] = {
    "restart": {"risk": "L2", "verb": "rollout restart"},
    "scale": {"risk": "L2", "verb": "scale"},
    "cordon": {"risk": "L2", "verb": "cordon"},
    "rollout_undo": {"risk": "L3", "verb": "rollout undo"},
}


def build_remediation_suggestions(
    service: str,
    namespace: str,
    actions: list[str],
) -> list[dict[str, Any]]:
    """Build dry-run kubectl suggestions for write-class remediations.

    Returns command suggestions only — for human approval. Nothing is executed,
    honoring the no-production-write boundary (Phase 2.2).
    """
    suggestions: list[dict[str, Any]] = []
    for action in actions:
        spec = _WRITE_SUGGESTIONS.get(action)
        if spec is None:
            continue
        target = f"deployment/{service}"
        if action == "cordon":
            target = "node/<node>"
        command = f"kubectl {spec['verb']} {target} -n {namespace} --dry-run=server"
        suggestions.append(
            {
                "action": action,
                "risk_level": spec["risk"],
                "command": command,
                "dry_run": True,
                "executed": False,
                "requires_approval": True,
            }
        )
    return suggestions


def build_k8s_backend(settings: Settings) -> K8sBackend:
    """Select the k8s backend from settings (default: fixture)."""
    backend = settings.k8s_backend.strip().lower()
    if backend == "fixture":
        return FixtureK8sBackend(fixture_path=settings.k8s_fixture_path)
    if backend == "live":
        return LiveK8sBackend(
            namespace=settings.k8s_namespace, timeout_seconds=settings.tool_timeout_seconds
        )
    msg = f"unknown k8s_backend '{settings.k8s_backend}'"
    raise ValueError(msg)


def _load_kubernetes_config(config_module: Any) -> None:
    """Load Kubernetes client config for both in-cluster and local execution."""
    try:
        config_module.load_incluster_config()
    except Exception:
        config_module.load_kube_config()
