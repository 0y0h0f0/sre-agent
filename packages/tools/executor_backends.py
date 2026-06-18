"""Remediation execution backend — Protocol + Fixture + Live.

Follows the same three-layer pattern as every other tool backend:
    ExecutorBackend (Protocol) → FixtureExecutorBackend → LiveK8sExecutorBackend

The Live backend is gated behind ``EXECUTOR_BACKEND=live`` and requires
explicit opt-in — the default ``fixture`` keeps tests deterministic and
local dev safe (same as every other backend in the project).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Protocol

from pydantic import BaseModel, Field

from packages.common.redaction import redact_text
from packages.common.settings import Settings

logger = logging.getLogger(__name__)

ExecutionStatus = str  # "succeeded" | "failed" | "partial" | "timeout"

#: Kubernetes resource name format (RFC 1123 DNS-1123 label).
_K8S_NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9\-]{0,61}[a-z0-9])?$")

#: Conservative live scale bounds. Fixture execution is unaffected.
LIVE_SCALE_MIN_REPLICAS = 0
LIVE_SCALE_MAX_REPLICAS = 50

#: Action types that are classified as rollback operations.
ROLLBACK_ACTION_TYPES: frozenset[str] = frozenset(
    {"rollback_release", "rollback_deployment", "scale_back", "revert_config"}
)

_ACTION_TYPE_ALIASES: dict[str, str] = {
    "rollback_deployment": "rollback_release",
}


def canonical_action_type(action_type: object) -> str:
    """Return the executor's canonical action type for supported aliases."""
    normalized = str(action_type or "").lower()
    return _ACTION_TYPE_ALIASES.get(normalized, normalized)


def is_valid_k8s_resource_name(value: str) -> bool:
    """Return whether ``value`` is a Kubernetes DNS-1123 resource name."""
    return bool(_K8S_NAME_RE.match(value))


def has_live_rollback_handler(action_type: str) -> bool:
    """Return whether live K8s rollback can handle ``action_type``."""
    return canonical_action_type(action_type) in _LIVE_ROLLBACK_HANDLERS


class ExecutionContext(BaseModel):
    """Context passed to every execution call for audit and targeting."""

    service: str = "unknown"
    incident_id: str = ""
    agent_run_id: str = ""
    namespace: str | None = None


class ExecutionResult(BaseModel):
    """Result of a single action execution or rollback.

    Serialises to a dict via ``model_dump()`` so it is drop-in compatible
    with the existing ``execution_result`` dict used in state.
    """

    status: ExecutionStatus = "succeeded"
    message: str = ""
    details: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


class ExecutorBackend(Protocol):
    """Execution backend contract.

    Every backend must implement ``execute`` and ``rollback``. The caller
    (``execute_action`` node) does not know which backend is active — it
    only depends on this Protocol.
    """

    name: str

    def execute(self, action: dict[str, Any], context: ExecutionContext) -> ExecutionResult:
        """Execute a single remediation action."""

    def rollback(
        self,
        action: dict[str, Any],
        snapshot: dict[str, Any],
        context: ExecutionContext,
    ) -> ExecutionResult:
        """Rollback a previously executed action using snapshot data."""


# ---------------------------------------------------------------------------
# Fixture (tests + local dev)
# ---------------------------------------------------------------------------

# Mirror the original MOCK_EXECUTOR_RESULTS shape so every existing action
# type has a deterministic result.
_FIXTURE_RESULTS: dict[str, ExecutionResult] = {
    "restart_pod": ExecutionResult(status="succeeded", message="mock pod restart completed"),
    "restart_deployment": ExecutionResult(
        status="succeeded", message="mock deployment restart completed"
    ),
    "restart_service": ExecutionResult(
        status="succeeded", message="mock service restart completed"
    ),
    "restart_statefulset": ExecutionResult(
        status="succeeded", message="mock statefulset restart completed"
    ),
    "pause_rollout": ExecutionResult(status="succeeded", message="mock rollout pause completed"),
    "resume_rollout": ExecutionResult(status="succeeded", message="mock rollout resume completed"),
    "scale_deployment": ExecutionResult(status="succeeded", message="mock scaling completed"),
    "rollback_release": ExecutionResult(status="succeeded", message="mock rollback completed"),
    "enable_rate_limit": ExecutionResult(status="succeeded", message="mock rate limit enabled"),
    "warmup_cache": ExecutionResult(status="succeeded", message="mock cache warming completed"),
    "create_ticket": ExecutionResult(status="succeeded", message="mock ticket created"),
    "adjust_connection_pool": ExecutionResult(status="succeeded", message="mock pool adjusted"),
    "increase_memory_limit": ExecutionResult(
        status="succeeded", message="mock memory limit increase completed"
    ),
    # Rollback-specific actions.
    "scale_back": ExecutionResult(status="succeeded", message="mock scale-back completed"),
    "revert_config": ExecutionResult(status="succeeded", message="mock config revert completed"),
    "cancel_deployment": ExecutionResult(
        status="succeeded", message="mock deployment cancellation completed"
    ),
}

_FIXTURE_FALLBACK = ExecutionResult(status="succeeded", message="mock execution completed")


class FixtureExecutorBackend:
    """Deterministic executor for tests and local dev (default)."""

    name = "fixture"

    def execute(self, action: dict[str, Any], context: ExecutionContext) -> ExecutionResult:
        atype = canonical_action_type(action.get("type"))
        return _FIXTURE_RESULTS.get(atype, _FIXTURE_FALLBACK)

    def rollback(
        self,
        action: dict[str, Any],
        snapshot: dict[str, Any],
        context: ExecutionContext,
    ) -> ExecutionResult:
        atype = canonical_action_type(action.get("type"))
        result = _FIXTURE_RESULTS.get(atype, _FIXTURE_FALLBACK)
        return ExecutionResult(
            status=result.status,
            message=f"mock rollback of {atype}: {result.message}",
            details={"snapshot_keys": list(snapshot.keys()) if snapshot else []},
        )


# ---------------------------------------------------------------------------
# Live K8s (production) — gated behind EXECUTOR_BACKEND=live
# ---------------------------------------------------------------------------


class LiveK8sExecutorBackend:
    """Executes remediation actions against a live Kubernetes cluster.

    Uses the ``kubernetes`` Python client (lazy import). Requires a valid
    kubeconfig or in-cluster ServiceAccount. Write operations are gated
    behind explicit ``EXECUTOR_BACKEND=live`` and the full guardrail →
    approval → second-confirmation chain.
    """

    name = "live"

    def __init__(
        self,
        *,
        namespace: str = "default",
        timeout_seconds: float = 30.0,
    ) -> None:
        self.namespace = _effective_live_namespace(namespace)
        self.timeout_seconds = timeout_seconds

    # ------------------------------------------------------------------
    # execute
    # ------------------------------------------------------------------

    def execute(self, action: dict[str, Any], context: ExecutionContext) -> ExecutionResult:
        atype = canonical_action_type(action.get("type"))
        target = _normalize_k8s_name(action.get("target", ""))
        raw_params = action.get("params")
        ns = _effective_live_namespace(context.namespace, self.namespace)

        # Validate K8s resource names to prevent path traversal.
        if not target or not _K8S_NAME_RE.match(target):
            return ExecutionResult(
                status="failed",
                message=f"invalid k8s resource name for target: {target}",
            )
        if ns and not _K8S_NAME_RE.match(ns):
            return ExecutionResult(
                status="failed",
                message=f"invalid k8s resource name for namespace: {ns}",
            )

        handler = _LIVE_HANDLERS.get(atype)
        if handler is None:
            return ExecutionResult(
                status="failed",
                message=f"unknown action type '{atype}' for live executor",
            )

        params_type_error = _params_type_error(raw_params)
        if params_type_error:
            return ExecutionResult(status="failed", message=params_type_error)
        params = dict(_to_dict(raw_params))
        params_error = _live_params_error(atype, params)
        if params_error is not None:
            return params_error

        try:
            return handler(atype, target, params, ns, self.timeout_seconds)
        except Exception as exc:
            logger.error(
                "live executor: action=%s target=%s ns=%s failed error_type=%s error=%s",
                atype,
                target,
                ns,
                type(exc).__name__,
                _safe_exception_message(exc),
            )
            return ExecutionResult(
                status="failed",
                message=f"live execution of '{atype}' failed",
                details={"error_type": type(exc).__name__},
            )

    # ------------------------------------------------------------------
    # rollback
    # ------------------------------------------------------------------

    def rollback(
        self,
        action: dict[str, Any],
        snapshot: dict[str, Any],
        context: ExecutionContext,
    ) -> ExecutionResult:
        atype = canonical_action_type(action.get("type"))
        target = _normalize_k8s_name(action.get("target", ""))
        raw_params = action.get("params")
        ns = _effective_live_namespace(context.namespace, self.namespace)

        # Validate K8s resource names to prevent path traversal.
        if not target or not _K8S_NAME_RE.match(target):
            return ExecutionResult(
                status="failed",
                message=f"invalid k8s resource name for target: {target}",
            )
        if ns and not _K8S_NAME_RE.match(ns):
            return ExecutionResult(
                status="failed",
                message=f"invalid k8s resource name for namespace: {ns}",
            )

        params_type_error = _params_type_error(raw_params)
        if params_type_error:
            return ExecutionResult(status="failed", message=params_type_error)
        params = dict(_to_dict(raw_params))

        # Prefer concrete values from the snapshot so the LLM does not
        # need to "remember" what the system looked like before.
        k8s_snap = snapshot.get("k8s") if isinstance(snapshot, dict) else {}
        if isinstance(k8s_snap, dict) and "error" not in k8s_snap:
            revision = k8s_snap.get("revision")
            replicas = k8s_snap.get("replicas")
            if (
                atype == "rollback_release"
                and params.get("to_revision") is None
                and revision is not None
            ):
                params["to_revision"] = revision
            if (
                atype in {"scale_back", "scale_deployment"}
                and params.get("replicas") is None
                and replicas is not None
            ):
                params["replicas"] = replicas

        handler = _LIVE_ROLLBACK_HANDLERS.get(atype)
        if handler is None:
            return ExecutionResult(
                status="failed",
                message=f"unknown rollback type '{atype}' for live executor",
            )

        params_error = _live_params_error(atype, params)
        if params_error is not None:
            return params_error

        try:
            return handler(atype, target, params, ns, self.timeout_seconds)
        except Exception as exc:
            logger.error(
                "live executor: rollback=%s target=%s ns=%s failed error_type=%s error=%s",
                atype,
                target,
                ns,
                type(exc).__name__,
                _safe_exception_message(exc),
            )
            return ExecutionResult(
                status="failed",
                message=f"live rollback of '{atype}' failed",
                details={"error_type": type(exc).__name__},
            )


# ---------------------------------------------------------------------------
# Live handler type alias and definitions
# ---------------------------------------------------------------------------

_LiveHandler = Callable[[str, str, dict[str, Any], str, float], ExecutionResult]


def _live_restart_pod(
    atype: str, target: str, params: dict[str, Any], ns: str, timeout: float
) -> ExecutionResult:
    """Trigger a rolling restart by patching the Deployment's pod template."""
    target, params, ns, preflight_error = _live_handler_preflight(atype, target, params, ns)
    if preflight_error is not None:
        return preflight_error
    _ensure_k8s_client()
    from kubernetes import client  # type: ignore[import-untyped]

    apps = client.AppsV1Api()
    body = {
        "spec": {
            "template": {
                "metadata": {
                    "annotations": {
                        "kubectl.kubernetes.io/restartedAt": _now_iso(),
                    }
                }
            }
        }
    }
    apps.patch_namespaced_deployment(name=target, namespace=ns, body=body, _request_timeout=timeout)
    return ExecutionResult(
        status="succeeded",
        message=f"restart triggered for deployment/{target} in {ns}",
        details={
            "resource": "deployment",
            "target": target,
            "namespace": ns,
            "patch": "pod_template_annotation",
            "annotation": "kubectl.kubernetes.io/restartedAt",
        },
    )


def _live_restart_statefulset(
    atype: str, target: str, params: dict[str, Any], ns: str, timeout: float
) -> ExecutionResult:
    """Trigger a StatefulSet rolling restart by patching its pod template."""
    target, params, ns, preflight_error = _live_handler_preflight(atype, target, params, ns)
    if preflight_error is not None:
        return preflight_error
    _ensure_k8s_client()
    from kubernetes import client  # type: ignore[import-untyped]

    body = {
        "spec": {
            "template": {
                "metadata": {
                    "annotations": {
                        "kubectl.kubernetes.io/restartedAt": _now_iso(),
                    }
                }
            }
        }
    }
    client.AppsV1Api().patch_namespaced_stateful_set(
        name=target,
        namespace=ns,
        body=body,
        _request_timeout=timeout,
    )
    return ExecutionResult(
        status="succeeded",
        message=f"restart triggered for statefulset/{target} in {ns}",
        details={
            "resource": "statefulset",
            "target": target,
            "namespace": ns,
            "patch": "pod_template_annotation",
            "annotation": "kubectl.kubernetes.io/restartedAt",
        },
    )


def _live_scale_deployment(
    atype: str, target: str, params: dict[str, Any], ns: str, timeout: float
) -> ExecutionResult:
    """Scale a Deployment to the requested replica count."""
    target, params, ns, preflight_error = _live_handler_preflight(atype, target, params, ns)
    if preflight_error is not None:
        return preflight_error
    raw_replicas = params.get("replicas")
    replicas, error_message = coerce_live_scale_replicas(raw_replicas)
    if error_message is not None:
        return ExecutionResult(
            status="failed",
            message=error_message,
            details={
                "min_replicas": LIVE_SCALE_MIN_REPLICAS,
                "max_replicas": LIVE_SCALE_MAX_REPLICAS,
            },
        )
    _ensure_k8s_client()
    from kubernetes import client  # type: ignore[import-untyped]

    apps = client.AppsV1Api()
    body = {"spec": {"replicas": replicas}}
    apps.patch_namespaced_deployment_scale(
        name=target, namespace=ns, body=body, _request_timeout=timeout
    )
    return ExecutionResult(
        status="succeeded",
        message=f"scaled deployment/{target} to {replicas} replicas in {ns}",
        details={
            "resource": "deployment",
            "target": target,
            "namespace": ns,
            "subresource": "scale",
            "replicas": replicas,
        },
    )


def _live_pause_rollout(
    atype: str, target: str, params: dict[str, Any], ns: str, timeout: float
) -> ExecutionResult:
    """Pause a Deployment rollout by setting spec.paused=true."""
    target, params, ns, preflight_error = _live_handler_preflight(atype, target, params, ns)
    if preflight_error is not None:
        return preflight_error
    _ensure_k8s_client()
    from kubernetes import client  # type: ignore[import-untyped]

    body = {"spec": {"paused": True}}
    client.AppsV1Api().patch_namespaced_deployment(
        name=target,
        namespace=ns,
        body=body,
        _request_timeout=timeout,
    )
    return ExecutionResult(
        status="succeeded",
        message=f"paused rollout for deployment/{target} in {ns}",
        details={
            "resource": "deployment",
            "target": target,
            "namespace": ns,
            "patch": "spec.paused",
            "paused": True,
        },
    )


def _live_resume_rollout(
    atype: str, target: str, params: dict[str, Any], ns: str, timeout: float
) -> ExecutionResult:
    """Resume a Deployment rollout by setting spec.paused=false."""
    target, params, ns, preflight_error = _live_handler_preflight(atype, target, params, ns)
    if preflight_error is not None:
        return preflight_error
    _ensure_k8s_client()
    from kubernetes import client  # type: ignore[import-untyped]

    body = {"spec": {"paused": False}}
    client.AppsV1Api().patch_namespaced_deployment(
        name=target,
        namespace=ns,
        body=body,
        _request_timeout=timeout,
    )
    return ExecutionResult(
        status="succeeded",
        message=f"resumed rollout for deployment/{target} in {ns}",
        details={
            "resource": "deployment",
            "target": target,
            "namespace": ns,
            "patch": "spec.paused",
            "paused": False,
        },
    )


def _live_rollback_release(
    atype: str, target: str, params: dict[str, Any], ns: str, timeout: float
) -> ExecutionResult:
    """Rollback a Deployment to a previous revision."""
    target, params, ns, preflight_error = _live_handler_preflight(atype, target, params, ns)
    if preflight_error is not None:
        return preflight_error
    to_revision, error_message = coerce_live_rollback_revision(params.get("to_revision"))
    if error_message is not None:
        return ExecutionResult(status="failed", message=error_message)
    _ensure_k8s_client()

    # POST to the deployment's rollback sub-resource.
    from kubernetes import client  # type: ignore[import-untyped]

    body: dict[str, Any] = {"name": target}
    if to_revision is not None:
        body["rollbackTo"] = {"revision": to_revision}

    client.AppsV1Api().api_client.call_api(
        f"/apis/apps/v1/namespaces/{ns}/deployments/{target}/rollback",
        "POST",
        body=body,
        auth_settings=["BearerToken"],
        _request_timeout=timeout,
    )
    rev_msg = f" to revision {to_revision}" if to_revision is not None else ""
    return ExecutionResult(
        status="succeeded",
        message=f"rollback deployment/{target}{rev_msg} in {ns}",
        details={
            "resource": "deployment",
            "target": target,
            "namespace": ns,
            "subresource": "rollback",
            "to_revision": to_revision,
        },
    )


def _live_scale_back(
    atype: str, target: str, params: dict[str, Any], ns: str, timeout: float
) -> ExecutionResult:
    """Scale back to a previous replica count (same mechanism as scale)."""
    return _live_scale_deployment(atype, target, params, ns, timeout)


def _live_restart_service(
    atype: str, target: str, params: dict[str, Any], ns: str, timeout: float
) -> ExecutionResult:
    """Restart all pods in a service by patching the owning Deployment."""
    return _live_restart_pod(atype, target, params, ns, timeout)


def _live_not_implemented(
    atype: str, target: str, params: dict[str, Any], ns: str, timeout: float
) -> ExecutionResult:
    logger.warning("live executor: action '%s' is not implemented", atype)
    return ExecutionResult(
        status="failed",
        message=f"live execution of '{atype}' is not implemented",
    )


_LIVE_HANDLERS: dict[str, _LiveHandler] = {
    "restart_pod": _live_restart_pod,
    "restart_deployment": _live_restart_pod,
    "restart_statefulset": _live_restart_statefulset,
    "scale_deployment": _live_scale_deployment,
    "pause_rollout": _live_pause_rollout,
    "resume_rollout": _live_resume_rollout,
    "rollback_release": _live_rollback_release,
    "scale_back": _live_scale_back,
    "restart_service": _live_restart_service,
    "adjust_connection_pool": _live_not_implemented,
    "warmup_cache": _live_not_implemented,
    "enable_rate_limit": _live_not_implemented,
    "cancel_deployment": _live_not_implemented,
    "revert_config": _live_not_implemented,
}

_LIVE_ROLLBACK_HANDLERS: dict[str, _LiveHandler] = {
    "scale_back": _live_scale_back,
    "rollback_release": _live_rollback_release,
    "scale_deployment": _live_scale_back,
    "revert_config": _live_not_implemented,
    "restart_pod": _live_not_implemented,
    "restart_deployment": _live_not_implemented,
    "restart_statefulset": _live_not_implemented,
    "restart_service": _live_not_implemented,
}

_LIVE_ALLOWED_PARAMS: dict[str, frozenset[str]] = {
    "restart_pod": frozenset(),
    "restart_deployment": frozenset(),
    "restart_service": frozenset(),
    "restart_statefulset": frozenset(),
    "pause_rollout": frozenset(),
    "resume_rollout": frozenset(),
    "scale_deployment": frozenset({"replicas"}),
    "scale_back": frozenset({"replicas"}),
    "rollback_release": frozenset({"to_revision"}),
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _params_type_error(value: object) -> str:
    if value is None or isinstance(value, (dict, BaseModel)):
        return ""
    return "live action params must be an object"


def _live_params_error(
    action_type: str,
    params: dict[str, Any],
) -> ExecutionResult | None:
    allowed = _LIVE_ALLOWED_PARAMS.get(action_type)
    if allowed is None:
        return None
    unexpected = sorted(set(params) - allowed)
    if not unexpected:
        return None
    allowed_params = sorted(allowed)
    return ExecutionResult(
        status="failed",
        message=f"live action '{action_type}' received unsupported params",
        details={
            "allowed_params": allowed_params,
            "unexpected_params": unexpected,
        },
    )


def _live_handler_preflight(
    action_type: str,
    target: object,
    params: object,
    namespace: object,
) -> tuple[str, dict[str, Any], str, ExecutionResult | None]:
    normalized_target = _normalize_k8s_name(target)
    normalized_namespace = _effective_live_namespace(namespace)
    if not normalized_target or not _K8S_NAME_RE.match(normalized_target):
        return (
            normalized_target,
            {},
            normalized_namespace,
            ExecutionResult(
                status="failed",
                message=f"invalid k8s resource name for target: {normalized_target}",
            ),
        )
    if not _K8S_NAME_RE.match(normalized_namespace):
        return (
            normalized_target,
            {},
            normalized_namespace,
            ExecutionResult(
                status="failed",
                message=f"invalid k8s resource name for namespace: {normalized_namespace}",
            ),
        )

    params_type_error = _params_type_error(params)
    if params_type_error:
        return (
            normalized_target,
            {},
            normalized_namespace,
            ExecutionResult(status="failed", message=params_type_error),
        )
    normalized_params = _to_dict(params)
    canonical_type = canonical_action_type(action_type)
    params_error = _live_params_error(canonical_type, normalized_params)
    return normalized_target, normalized_params, normalized_namespace, params_error


def _to_dict(value: object) -> dict[str, Any]:
    """Coerce params to a dict safely."""
    if isinstance(value, dict):
        return dict(value)  # copy to prevent shared reference mutation
    if isinstance(value, BaseModel):
        return value.model_dump()
    if value is not None:
        logger.warning("_to_dict received unexpected type: %s", type(value))
    return {}


def _normalize_k8s_name(value: object) -> str:
    return str(value or "").strip()


def _effective_live_namespace(*candidates: object) -> str:
    for candidate in candidates:
        namespace = _normalize_k8s_name(candidate)
        if namespace:
            return namespace
    return "default"


def _safe_exception_message(exc: Exception) -> str:
    return redact_text(str(exc)).redacted_text


def coerce_live_scale_replicas(value: object) -> tuple[int | None, str | None]:
    """Return a bounded live Deployment replica count or an audit-friendly error."""
    if value is None:
        return None, "scale_deployment requires 'replicas' in params"
    if isinstance(value, bool):
        return None, "scale_deployment replicas must be an integer"
    if isinstance(value, int):
        replicas = value
    elif isinstance(value, str) and value.strip().isdigit():
        replicas = int(value.strip())
    else:
        return None, "scale_deployment replicas must be an integer"
    if replicas < LIVE_SCALE_MIN_REPLICAS or replicas > LIVE_SCALE_MAX_REPLICAS:
        return None, (
            "scale_deployment replicas must be between "
            f"{LIVE_SCALE_MIN_REPLICAS} and {LIVE_SCALE_MAX_REPLICAS}"
        )
    return replicas, None


def coerce_live_rollback_revision(value: object) -> tuple[int | None, str | None]:
    """Return a positive Deployment rollback revision or an audit-friendly error."""
    if value is None:
        return None, None
    if isinstance(value, bool):
        return None, "rollback_release to_revision must be a positive integer"
    if isinstance(value, int):
        revision = value
    elif isinstance(value, str) and value.strip().isdigit():
        revision = int(value.strip())
    else:
        return None, "rollback_release to_revision must be a positive integer"
    if revision <= 0:
        return None, "rollback_release to_revision must be a positive integer"
    return revision, None


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _ensure_k8s_client() -> None:
    """Lazy-import and configure the kubernetes client."""
    try:
        from kubernetes import config  # type: ignore[import-untyped]
    except ImportError as exc:
        msg = "kubernetes client not installed; run: pip install kubernetes"
        raise RuntimeError(msg) from exc
    try:
        config.load_incluster_config()
    except Exception as incluster_exc:
        try:
            config.load_kube_config()
        except Exception as kubeconfig_exc:
            incluster_error = _safe_exception_message(incluster_exc)
            kubeconfig_error = _safe_exception_message(kubeconfig_exc)
            raise RuntimeError(
                "Cannot configure Kubernetes client: "
                f"in-cluster config failed: {incluster_error}; "
                f"kubeconfig failed: {kubeconfig_error}"
            ) from kubeconfig_exc


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def build_executor_backend(settings: Settings) -> ExecutorBackend:
    """Build the executor backend from settings.

    Returns a ``FixtureExecutorBackend`` when ``EXECUTOR_BACKEND`` is
    ``"fixture"`` (the default) and a ``LiveK8sExecutorBackend`` when set
    to ``"live"``.
    """
    backend = settings.executor_backend.strip().lower()
    if backend == "fixture":
        return FixtureExecutorBackend()
    if backend == "live":
        return LiveK8sExecutorBackend(
            namespace=_effective_live_executor_namespace(settings),
            timeout_seconds=settings.executor_timeout_seconds,
        )
    msg = f"unknown executor_backend '{settings.executor_backend}'"
    raise ValueError(msg)


def _effective_live_executor_namespace(settings: Settings) -> str:
    return _effective_live_namespace(
        getattr(settings, "executor_k8s_namespace", ""),
        getattr(settings, "k8s_namespace", ""),
    )
