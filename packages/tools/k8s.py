"""Kubernetes read-only diagnosis tool (roadmap Phase 2.2).

MVP scope is read-only: describe pod, logs, events, rollout status, and
Deployment/StatefulSet snapshots. The fixture backend keeps tests deterministic
and local dev offline; the live backend uses the kubernetes client against an
explicitly configured cluster.

Hard boundary (``00-overview/scope.md``): this tool never performs production
writes. Write-class remediations (cordon/restart/scale/rollout-undo) are only
ever emitted as dry-run *suggestions* via :func:`build_remediation_suggestions`
— they are returned for human approval, never executed here.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, Field, field_validator

from packages.common.redaction import redact_text
from packages.common.settings import Settings
from packages.tools.base import ToolResult, ToolStatus, compact_summary, elapsed_ms, start_timer

# Read-only diagnosis operations. ``operation`` is a free str (not a Literal) so
# the read-only check in K8sDiagnosticsTool.run is the single, testable
# enforcement point — a write-class operation reaches the tool and is refused.
_READ_ONLY_OPERATIONS: frozenset[str] = frozenset(
    {"describe_pod", "logs", "events", "rollout_status", "get_deployment", "get_statefulset"}
)
_K8S_DNS_LABEL_RE = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")
_K8S_LABEL_VALUE_RE = re.compile(r"^[A-Za-z0-9]([-A-Za-z0-9_.]{0,61}[A-Za-z0-9])?$")
_POD_SELECTOR_LABELS: tuple[str, ...] = (
    "app.kubernetes.io/name",
    "app",
    "service",
    "deployment",
)
_DEFAULT_K8S_NAMESPACE = "default"


class K8sQuery(BaseModel):
    """Read-only Kubernetes diagnostics query.

    ``operation`` remains a plain string so tests can verify unsupported write
    verbs are rejected by the tool, not by Pydantic before reaching the boundary.
    """

    service: str = Field(min_length=1)
    operation: str = "describe_pod"
    namespace: str = ""
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
        # Fixture payloads mirror the live backend's operation/payload shape so
        # agent nodes do not need separate code paths for local demo and live.
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
        self.namespace = _effective_k8s_namespace(namespace)
        self.timeout_seconds = timeout_seconds

    def fetch(self, query: K8sQuery) -> dict[str, Any]:
        if query.operation not in _READ_ONLY_OPERATIONS:
            msg = f"refusing non-read-only k8s operation '{query.operation}'"
            raise ValueError(msg)
        ns = _effective_k8s_namespace(query.namespace, self.namespace)
        validation_error = _live_query_validation_error(query, namespace=ns)
        if validation_error:
            # Return a structured payload instead of raising so K8sDiagnosticsTool
            # can mark the result degraded and avoid creating misleading evidence.
            return {
                "operation": query.operation,
                "payload": {
                    "error": validation_error,
                },
            }
        try:
            from kubernetes import client, config  # type: ignore[import-untyped]
        except ImportError as exc:  # pragma: no cover - optional dependency
            msg = "kubernetes client not installed; use k8s_backend=fixture"
            raise RuntimeError(msg) from exc

        _load_kubernetes_config(config)  # pragma: no cover - requires a real cluster
        core = client.CoreV1Api()  # pragma: no cover
        timeout = self.timeout_seconds  # pragma: no cover
        if query.operation == "events":  # pragma: no cover
            try:
                payload = _event_messages(core, query=query, namespace=ns, timeout=timeout)
            except Exception as exc:
                payload = _live_k8s_error_payload(exc)
            return {
                "operation": "events",
                "payload": payload,
            }
        if query.operation == "logs":  # pragma: no cover
            try:
                # Missing explicit pod names are resolved with read-only label
                # selectors scoped to the namespace; no namespace-wide log scan.
                pod_name = _resolve_pod_name(core, query=query, namespace=ns, timeout=timeout)
                if not pod_name:
                    return {"operation": "logs", "payload": {}}
                logs = core.read_namespaced_pod_log(
                    pod_name, ns, tail_lines=100, _request_timeout=timeout
                )
                return {
                    "operation": "logs",
                    "pod": pod_name,
                    "payload": redact_text(str(logs or "")).redacted_text,
                }
            except Exception as exc:
                payload = _live_k8s_error_payload(exc)
                result: dict[str, Any] = {"operation": "logs", "payload": payload}
                if query.pod:
                    result["pod"] = query.pod
                return result
        if query.operation == "describe_pod":  # pragma: no cover
            try:
                # describe_pod returns a curated summary only, never raw Pod spec
                # fields such as env, args, annotations, or mounted secrets.
                pod_name = _resolve_pod_name(core, query=query, namespace=ns, timeout=timeout)
                if not pod_name:
                    return {"operation": "describe_pod", "payload": {}}
                pod = core.read_namespaced_pod(pod_name, ns, _request_timeout=timeout)
                return {
                    "operation": "describe_pod",
                    "pod": pod_name,
                    "payload": _pod_describe_payload(pod, name=pod_name, namespace=ns),
                }
            except Exception as exc:
                payload = _live_k8s_error_payload(exc)
                result = {"operation": "describe_pod", "payload": payload}
                if query.pod:
                    result["pod"] = query.pod
                return result
        if query.operation in {"get_deployment", "rollout_status"}:  # pragma: no cover
            apps = client.AppsV1Api()
            try:
                deploy = apps.read_namespaced_deployment(
                    query.service, ns, _request_timeout=timeout
                )
                payload = _deployment_payload(deploy, name=query.service, namespace=ns)
                if query.operation == "rollout_status":
                    payload = {
                        **payload,
                        "desired_replicas": payload.get("replicas"),
                        "status": _deployment_rollout_status(payload),
                    }
                return {"operation": query.operation, "payload": payload}
            except Exception as exc:
                return {"operation": query.operation, "payload": _live_k8s_error_payload(exc)}
        if query.operation == "get_statefulset":  # pragma: no cover
            apps = client.AppsV1Api()
            try:
                statefulset = apps.read_namespaced_stateful_set(
                    query.service, ns, _request_timeout=timeout
                )
                payload = _statefulset_payload(
                    statefulset, name=query.service, namespace=ns
                )
                return {"operation": "get_statefulset", "payload": payload}
            except Exception as exc:
                return {"operation": "get_statefulset", "payload": _live_k8s_error_payload(exc)}
        return {"operation": query.operation, "payload": {}}  # pragma: no cover


def _deployment_payload(deploy: Any, *, name: str, namespace: str) -> dict[str, Any]:
    """Return a safe Deployment summary for snapshots and rollout verify."""
    metadata = getattr(deploy, "metadata", None)
    spec = getattr(deploy, "spec", None)
    status = getattr(deploy, "status", None)
    annotations = getattr(metadata, "annotations", None) or {}
    return {
        "name": name,
        "namespace": namespace,
        "replicas": getattr(spec, "replicas", None) if spec else None,
        "paused": bool(getattr(spec, "paused", False)) if spec else False,
        "revision": annotations.get("deployment.kubernetes.io/revision"),
        "image": _first_container_image(spec),
        "ready_replicas": getattr(status, "ready_replicas", None) if status else None,
        "available_replicas": getattr(status, "available_replicas", None) if status else None,
        "updated_replicas": getattr(status, "updated_replicas", None) if status else None,
        "unavailable_replicas": getattr(status, "unavailable_replicas", None)
        if status
        else None,
        "observed_generation": getattr(status, "observed_generation", None) if status else None,
        "generation": getattr(metadata, "generation", None) if metadata else None,
        "conditions": _conditions_payload(status),
    }


def _statefulset_payload(statefulset: Any, *, name: str, namespace: str) -> dict[str, Any]:
    """Return a safe StatefulSet summary for snapshot/verify flows."""
    metadata = getattr(statefulset, "metadata", None)
    spec = getattr(statefulset, "spec", None)
    status = getattr(statefulset, "status", None)
    payload = {
        "kind": "StatefulSet",
        "name": name,
        "namespace": namespace,
        "replicas": getattr(spec, "replicas", None) if spec else None,
        "desired_replicas": getattr(spec, "replicas", None) if spec else None,
        "image": _first_container_image(spec),
        "ready_replicas": getattr(status, "ready_replicas", None) if status else None,
        "current_replicas": getattr(status, "current_replicas", None) if status else None,
        "updated_replicas": getattr(status, "updated_replicas", None) if status else None,
        "current_revision": getattr(status, "current_revision", None) if status else None,
        "update_revision": getattr(status, "update_revision", None) if status else None,
        "observed_generation": getattr(status, "observed_generation", None) if status else None,
        "generation": getattr(metadata, "generation", None) if metadata else None,
        "conditions": _conditions_payload(status),
    }
    payload["status"] = _statefulset_rollout_status(payload)
    return payload


def _event_messages(core: Any, *, query: K8sQuery, namespace: str, timeout: float) -> list[str]:
    """Read event messages for the workload/pod only, with redaction."""
    names = _event_target_names(core, query=query, namespace=namespace, timeout=timeout)
    messages: list[str] = []
    seen: set[str] = set()
    for name in names:
        events = core.list_namespaced_event(
            namespace,
            field_selector=f"involvedObject.name={name}",
            _request_timeout=timeout,
        )
        for event in (getattr(events, "items", None) or [])[:50]:
            message = redact_text(str(getattr(event, "message", "") or "")).redacted_text
            if not message or message in seen:
                continue
            messages.append(message)
            seen.add(message)
            if len(messages) >= 50:
                return messages
    return messages


def _event_target_names(
    core: Any,
    *,
    query: K8sQuery,
    namespace: str,
    timeout: float,
) -> list[str]:
    """Build a small ordered list of workload and resolved pod names."""
    names: list[str] = []
    for candidate in (query.pod, query.service):
        if candidate and _K8S_LABEL_VALUE_RE.match(candidate) and candidate not in names:
            names.append(candidate)
    pod_name = _resolve_pod_name(core, query=query, namespace=namespace, timeout=timeout)
    if pod_name and pod_name not in names:
        names.append(pod_name)
    return names


def _pod_describe_payload(pod: Any, *, name: str, namespace: str) -> dict[str, Any]:
    """Extract non-secret Pod status fields for diagnostics evidence."""
    spec = getattr(pod, "spec", None)
    status = getattr(pod, "status", None)
    return {
        "name": name,
        "namespace": namespace,
        "phase": getattr(status, "phase", None) if status else None,
        "node_name": getattr(spec, "node_name", None) if spec else None,
        "restart_count": _pod_restart_count(status),
        "containers": _pod_container_summaries(pod),
        "conditions": _conditions_payload(status),
    }


def _pod_restart_count(status: Any) -> int:
    return sum(
        _coerce_int(getattr(container, "restart_count", None)) or 0
        for container in (getattr(status, "container_statuses", None) or [])
    )


def _pod_container_summaries(pod: Any) -> list[dict[str, Any]]:
    """Return container status summaries without env/args/raw spec data."""
    spec = getattr(pod, "spec", None)
    status = getattr(pod, "status", None)
    spec_by_name = {
        str(getattr(container, "name", "") or ""): container
        for container in (getattr(spec, "containers", None) or [])
    }
    summaries: list[dict[str, Any]] = []
    for container_status in getattr(status, "container_statuses", None) or []:
        name = str(getattr(container_status, "name", "") or "")
        container_spec = spec_by_name.get(name)
        summaries.append(
            {
                "name": name,
                "image": getattr(container_spec, "image", None) if container_spec else None,
                "ready": bool(getattr(container_status, "ready", False)),
                "restart_count": getattr(container_status, "restart_count", None),
                "state": _container_state_name(getattr(container_status, "state", None)),
                "reason": _container_state_reason(getattr(container_status, "state", None)),
            }
        )
    return summaries


def _container_state_name(state: Any) -> str:
    for name in ("waiting", "running", "terminated"):
        if getattr(state, name, None) is not None:
            return name
    return ""


def _container_state_reason(state: Any) -> str:
    for name in ("waiting", "terminated"):
        value = getattr(state, name, None)
        reason = getattr(value, "reason", None) if value is not None else None
        if reason:
            return str(reason)
    return ""


def _resolve_pod_name(core: Any, *, query: K8sQuery, namespace: str, timeout: float) -> str:
    """Resolve one non-terminal pod in the target namespace using common labels."""
    if query.pod:
        return query.pod
    if not _K8S_LABEL_VALUE_RE.match(query.service):
        return ""

    for label in _POD_SELECTOR_LABELS:
        pods = core.list_namespaced_pod(
            namespace,
            label_selector=f"{label}={query.service}",
            limit=10,
            _request_timeout=timeout,
        )
        candidates = getattr(pods, "items", None) or []
        pod_name = _first_running_pod_name(candidates)
        if pod_name:
            return pod_name
    return ""


def _first_running_pod_name(pods: list[Any]) -> str:
    """Prefer running pods, but allow pending pods over terminal ones."""
    ordered = sorted(pods, key=lambda pod: (_pod_phase(pod) != "Running", _pod_name(pod)))
    for pod in ordered:
        name = _pod_name(pod)
        phase = _pod_phase(pod)
        if name and phase not in {"Succeeded", "Failed"}:
            return name
    return ""


def _pod_name(pod: Any) -> str:
    metadata = getattr(pod, "metadata", None)
    return str(getattr(metadata, "name", "") or "")


def _pod_phase(pod: Any) -> str:
    status = getattr(pod, "status", None)
    return str(getattr(status, "phase", "") or "")


def _first_container_image(spec: Any) -> str | None:
    template = getattr(spec, "template", None)
    pod_spec = getattr(template, "spec", None)
    containers = getattr(pod_spec, "containers", None) or []
    if not containers:
        return None
    return getattr(containers[0], "image", None)


def _conditions_payload(status: Any) -> list[dict[str, str]]:
    return [
        {
            "type": str(getattr(condition, "type", "")),
            "status": str(getattr(condition, "status", "")),
        }
        for condition in (getattr(status, "conditions", None) or [])
    ]


def _deployment_rollout_status(payload: dict[str, Any]) -> str:
    """Map Deployment status fields into verify-friendly rollout states."""
    for condition in payload.get("conditions", []) or []:
        ctype = str(condition.get("type", ""))
        cstatus = str(condition.get("status", "")).lower()
        if ctype == "ReplicaFailure" and cstatus == "true":
            return "failed"
        if ctype == "Progressing" and cstatus == "false":
            return "failed"

    desired = _coerce_int(payload.get("replicas"))
    ready = _coerce_int(payload.get("ready_replicas"))
    available = _coerce_int(payload.get("available_replicas"))
    updated = _coerce_int(payload.get("updated_replicas"))
    observed_generation = _coerce_int(payload.get("observed_generation"))
    generation = _coerce_int(payload.get("generation"))

    if payload.get("paused") is True:
        return "paused"
    if (
        generation is not None
        and observed_generation is not None
        and observed_generation < generation
    ):
        return "progressing"
    if desired == 0:
        return "complete"
    if desired is not None and desired > 0:
        if (
            ready is not None
            and available is not None
            and updated is not None
            and ready >= desired
            and available >= desired
            and updated >= desired
        ):
            return "complete"
        if (ready or 0) > 0 or (updated or 0) > 0:
            return "progressing"
    return "pending"


def _statefulset_rollout_status(payload: dict[str, Any]) -> str:
    """Map StatefulSet status fields into verify-friendly rollout states."""
    for condition in payload.get("conditions", []) or []:
        ctype = str(condition.get("type", ""))
        cstatus = str(condition.get("status", "")).lower()
        if ctype in {"ReplicaFailure", "Failed"} and cstatus == "true":
            return "failed"
        if ctype == "Progressing" and cstatus == "false":
            return "failed"

    desired = _coerce_int(payload.get("replicas"))
    ready = _coerce_int(payload.get("ready_replicas"))
    current = _coerce_int(payload.get("current_replicas"))
    updated = _coerce_int(payload.get("updated_replicas"))
    observed_generation = _coerce_int(payload.get("observed_generation"))
    generation = _coerce_int(payload.get("generation"))
    current_revision = str(payload.get("current_revision") or "")
    update_revision = str(payload.get("update_revision") or "")
    # StatefulSet readiness alone is not enough: if current_revision has not
    # caught up to update_revision, the restart/update is still progressing.
    revisions_match = (
        not current_revision
        or not update_revision
        or current_revision == update_revision
    )

    if (
        generation is not None
        and observed_generation is not None
        and observed_generation < generation
    ):
        return "progressing"
    if desired == 0:
        return "complete"
    if desired is not None and desired > 0:
        ready_complete = ready is not None and ready >= desired
        updated_complete = updated is not None and updated >= desired
        current_complete = current is not None and current >= desired
        if ready_complete and revisions_match and (updated_complete or current_complete):
            return "complete"
        if (ready or 0) > 0 or (current or 0) > 0 or (updated or 0) > 0:
            return "progressing"
    return "pending"


def _coerce_int(value: object) -> int | None:
    """Coerce Kubernetes numeric fields while rejecting bools."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _is_k8s_dns_label(value: str) -> bool:
    return bool(_K8S_DNS_LABEL_RE.match(value))


def _is_k8s_resource_name(value: str) -> bool:
    if not value or len(value) > 253:
        return False
    return all(_is_k8s_dns_label(part) for part in value.split("."))


def _live_query_validation_error(query: K8sQuery, *, namespace: str) -> str:
    """Validate live-read names before calling the Kubernetes API."""
    if not _is_k8s_dns_label(namespace):
        return "invalid_namespace"
    if query.pod and not _is_k8s_resource_name(query.pod):
        return "invalid_pod_name"
    if query.operation in {"get_deployment", "rollout_status", "get_statefulset"}:
        if not _is_k8s_resource_name(query.service):
            return "invalid_resource_name"
    return ""


def _live_k8s_error_payload(exc: Exception) -> dict[str, str | int]:
    """Convert Kubernetes client exceptions into safe structured payloads."""
    error = _live_k8s_error_code(exc)
    status_code = _coerce_int(getattr(exc, "status", None))
    payload: dict[str, str | int] = {"error": error}
    if status_code is not None:
        payload["status_code"] = status_code
    return payload


def _live_k8s_error_code(exc: Exception) -> str:
    """Classify Kubernetes read failures without exposing raw exception text."""
    status_code = _coerce_int(getattr(exc, "status", None))
    if status_code == 404:
        return "not_found"
    if status_code == 403:
        return "forbidden"
    if status_code == 401:
        return "unauthorized"
    if status_code == 429:
        return "rate_limited"
    if status_code in {408, 504}:
        return "timeout"
    if status_code is not None and status_code >= 500:
        return "api_error"

    exc_name = type(exc).__name__.lower()
    if "timeout" in exc_name:
        return "timeout"
    if "forbidden" in exc_name or "permission" in exc_name:
        return "forbidden"
    return "read_failed"


def _effective_k8s_namespace(*candidates: object) -> str:
    """Choose the first configured namespace or the project default."""
    for candidate in candidates:
        namespace = str(candidate or "").strip()
        if namespace:
            return namespace
    return _DEFAULT_K8S_NAMESPACE


class K8sDiagnosticsTool:
    """BaseTool wrapper around fixture/live read-only Kubernetes backends."""

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
        # Redact before building the backend query. A malformed alert field must
        # not leak secrets into live API params, summaries, evidence, or errors.
        public_service = _redact_query_text(k8s_query.service)
        public_operation = _redact_query_text(k8s_query.operation)
        public_namespace = _redact_query_text(
            _effective_k8s_namespace(
                k8s_query.namespace,
                getattr(self.backend, "namespace", ""),
            )
        )
        public_query = k8s_query.model_copy(
            update={
                "service": public_service,
                "operation": public_operation,
                "namespace": public_namespace,
            }
        )
        started_at = start_timer()
        if k8s_query.operation not in _READ_ONLY_OPERATIONS:
            # Unsupported operations fail explicitly. They are not "degraded"
            # because the tool itself is healthy; the request violated policy.
            return ToolResult(
                status="failed",
                data={},
                summary=f"refused non-read-only k8s operation '{public_operation}'",
                duration_ms=elapsed_ms(started_at),
                error_message="k8s tool is read-only",
            )
        try:
            result_data = self.backend.fetch(public_query)
            payload = result_data.get("payload")
            payload_error = _payload_error(payload)
            # Error payloads from live validation/API reads are intentionally not
            # emitted as evidence. They are useful status, not positive facts.
            has_data = bool(payload) and payload_error is None
            status: ToolStatus = "succeeded" if has_data else "degraded"
            return ToolResult(
                status=status,
                data=result_data,
                summary=compact_summary(
                    {
                        "service": public_service,
                        "namespace": public_namespace,
                        "op": public_operation,
                        "has_data": has_data,
                    }
                ),
                evidence=[
                    {
                        "type": "k8s",
                        "source": self.backend.name,
                        "title": f"{public_operation} for {public_service}",
                        "payload": result_data,
                    }
                ]
                if has_data
                else [],
                duration_ms=elapsed_ms(started_at),
                error_message=None
                if has_data
                else _k8s_error_message(payload_error),
            )
        except Exception as exc:
            return ToolResult(
                status="degraded",
                data={},
                summary=f"k8s backend unavailable for {public_service}",
                duration_ms=elapsed_ms(started_at),
                error_message=redact_text(str(exc)).redacted_text,
            )


def _payload_error(payload: object) -> str | None:
    """Extract backend error marker from a structured payload."""
    if isinstance(payload, dict):
        error = payload.get("error")
        if error:
            return str(error)
    return None


def _k8s_error_message(payload_error: str | None) -> str:
    """Return the ToolResult error message for empty/degraded payloads."""
    if payload_error:
        return f"k8s backend returned error: {payload_error}"
    return "empty k8s result"


def _redact_query_text(value: str) -> str:
    """Redact user/alert-derived query text before public output or live calls."""
    return redact_text(value).redacted_text


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
        return
    except Exception as incluster_exc:
        try:
            config_module.load_kube_config()
            return
        except Exception as kubeconfig_exc:
            incluster_error = redact_text(str(incluster_exc)).redacted_text
            kubeconfig_error = redact_text(str(kubeconfig_exc)).redacted_text
            raise RuntimeError(
                "Cannot configure Kubernetes client: "
                f"in-cluster config failed: {incluster_error}; "
                f"kubeconfig failed: {kubeconfig_error}"
            ) from kubeconfig_exc
