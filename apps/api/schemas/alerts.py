from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from apps.api.schemas.common import Severity
from packages.common.time import utc_now

AlertSource = Literal[
    "alertmanager",
    "pagerduty",
    "grafana",
    "datadog",
    "custom",
    "mock",
]

SOURCE_VALUES = {"alertmanager", "pagerduty", "grafana", "datadog", "custom", "mock"}
SEVERITY_MAP = {
    "p1": "P1",
    "critical": "P1",
    "crit": "P1",
    "page": "P1",
    "emergency": "P1",
    "alert": "P1",
    "p2": "P2",
    "error": "P2",
    "warning": "P2",
    "warn": "P2",
    "high": "P2",
    "p3": "P3",
    "medium": "P3",
    "minor": "P3",
    "low": "P3",
    "p4": "P4",
    "info": "P4",
    "informational": "P4",
    "ok": "P4",
    "resolved": "P4",
}


class AlertCreateRequest(BaseModel):
    source: AlertSource
    fingerprint: str = Field(min_length=1, max_length=255)
    service: str = Field(min_length=1, max_length=128)
    severity: Severity
    alert_name: str = Field(min_length=1, max_length=255)
    starts_at: datetime
    ends_at: datetime | None = None
    labels: dict[str, Any] = Field(default_factory=dict)
    annotations: dict[str, Any] = Field(default_factory=dict)
    raw_payload: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def normalize_provider_payload(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        payload = dict(value)
        if _looks_unified(payload):
            raw = {k: v for k, v in payload.items() if k != "raw_payload"}
            payload.setdefault("raw_payload", _json_safe(raw))
            payload["source"] = _normalize_source(payload.get("source"))
            payload["severity"] = _normalize_severity(payload.get("severity"))
            return payload

        source = _normalize_source(payload.get("source") or _infer_source(payload))
        raw_payload = _json_safe(payload)
        if source == "mock":
            payload.setdefault("raw_payload", raw_payload)
            return payload
        if source == "alertmanager":
            normalized = _from_alertmanager(payload)
        elif source == "pagerduty":
            normalized = _from_pagerduty(payload)
        elif source == "grafana":
            normalized = _from_grafana(payload)
        elif source == "datadog":
            normalized = _from_datadog(payload)
        else:
            normalized = _from_custom(payload)
        normalized["source"] = source
        normalized["raw_payload"] = raw_payload
        return normalized

    @field_validator("fingerprint", "service", "alert_name")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            msg = "field must not be blank"
            raise ValueError(msg)
        return stripped

    @field_validator("ends_at")
    @classmethod
    def validate_ends_at_after_starts_at(cls, value: datetime | None, info: Any) -> datetime | None:
        if value is not None and "starts_at" in info.data:
            starts_at = info.data["starts_at"]
            if value <= starts_at:
                msg = "ends_at must be after starts_at"
                raise ValueError(msg)
        return value


class AlertCreateResponse(BaseModel):
    incident_id: str
    agent_run_id: str
    celery_task_id: str
    status: str  # "queued" for new incidents; existing status when deduplicated
    deduplicated: bool


def _looks_unified(payload: dict[str, Any]) -> bool:
    required = {"source", "fingerprint", "service", "severity", "alert_name", "starts_at"}
    return required.issubset(payload.keys())


def _normalize_source(value: Any) -> str:
    source = str(value or "custom").strip().lower().replace("-", "_")
    return source if source in SOURCE_VALUES else "custom"


def _normalize_severity(value: Any) -> str:
    text = str(value or "P3").strip()
    upper = text.upper()
    if upper in {"P1", "P2", "P3", "P4"}:
        return upper
    return SEVERITY_MAP.get(text.lower(), "P3")


def _json_safe(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    return value


def _string(value: Any, default: str = "") -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text or default


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _first_alert(payload: dict[str, Any]) -> dict[str, Any]:
    alerts = _list(payload.get("alerts"))
    first = alerts[0] if alerts and isinstance(alerts[0], dict) else {}
    return first


def _starts_at(*values: Any) -> Any:
    for value in values:
        if value:
            return value
    return utc_now()


def _ends_at(value: Any) -> Any:
    text = str(value or "").strip()
    if not text or text.startswith(("0001-", "0000-")):
        return None
    return value


def _infer_source(payload: dict[str, Any]) -> str:
    if "event" in payload and isinstance(payload.get("event"), dict):
        return "pagerduty"
    if "alert_id" in payload or "alert_title" in payload:
        return "datadog"
    if "orgId" in payload or "ruleUrl" in payload:
        return "grafana"
    if "commonLabels" in payload and "alerts" in payload:
        return "alertmanager"
    return "custom"


def _from_alertmanager(payload: dict[str, Any]) -> dict[str, Any]:
    first = _first_alert(payload)
    # Merge groupLabels as well — "service" is commonly placed there
    # by kube-prometheus-stack and other common Alertmanager configurations.
    labels = {
        **_dict(payload.get("groupLabels")),
        **_dict(payload.get("commonLabels")),
        **_dict(first.get("labels")),
    }
    annotations = {**_dict(payload.get("commonAnnotations")), **_dict(first.get("annotations"))}
    alert_name = _string(labels.get("alertname") or annotations.get("summary"), "AlertmanagerAlert")
    service = _string(labels.get("service") or labels.get("job") or labels.get("app"), "unknown")
    # Derive a stable fingerprint from the service+alertname pair when the
    # per-alert fingerprint is absent (some Alertmanager versions omit it).
    fingerprint = _string(
        first.get("fingerprint") or labels.get("fingerprint"),
        f"alertmanager:{service}:{alert_name}",
    )
    return {
        "fingerprint": fingerprint,
        "service": service,
        "severity": _normalize_severity(labels.get("severity") or annotations.get("severity")),
        "alert_name": alert_name,
        "starts_at": _starts_at(first.get("startsAt"), payload.get("starts_at")),
        "ends_at": _ends_at(first.get("endsAt") or payload.get("ends_at")),
        "labels": labels,
        "annotations": annotations,
    }


def _from_pagerduty(payload: dict[str, Any]) -> dict[str, Any]:
    event = _dict(payload.get("event"))
    data = _dict(event.get("data"))
    service_obj = _dict(data.get("service"))
    labels = {
        "event_type": event.get("event_type"),
        "urgency": data.get("urgency"),
        "incident_id": data.get("id"),
    }
    annotations = {
        "summary": data.get("summary") or event.get("summary"),
        "html_url": data.get("html_url") or data.get("self"),
    }
    service = _string(
        data.get("service_name") or service_obj.get("summary") or service_obj.get("id"),
        "unknown",
    )
    alert_name = _string(
        data.get("title") or data.get("summary") or event.get("event_type"),
        "PagerDutyAlert",
    )
    fingerprint = _string(
        data.get("dedup_key") or data.get("incident_key") or data.get("id") or event.get("id"),
        f"pagerduty:{service}:{alert_name}",
    )
    return {
        "fingerprint": fingerprint,
        "service": service,
        "severity": _normalize_severity(data.get("severity") or data.get("urgency")),
        "alert_name": alert_name,
        "starts_at": _starts_at(
            data.get("created_at"),
            event.get("occurred_at"),
            payload.get("created_at"),
        ),
        "labels": {k: v for k, v in labels.items() if v is not None},
        "annotations": {k: v for k, v in annotations.items() if v is not None},
    }


def _from_grafana(payload: dict[str, Any]) -> dict[str, Any]:
    first = _first_alert(payload)
    labels = {**_dict(payload.get("commonLabels")), **_dict(first.get("labels"))}
    annotations = {**_dict(payload.get("commonAnnotations")), **_dict(first.get("annotations"))}
    alert_name = _string(
        labels.get("alertname") or payload.get("title") or annotations.get("summary"),
        "GrafanaAlert",
    )
    service = _string(labels.get("service") or labels.get("job") or labels.get("app"), "unknown")
    fingerprint = _string(
        first.get("fingerprint") or payload.get("groupKey") or payload.get("ruleUrl"),
        f"grafana:{service}:{alert_name}",
    )
    return {
        "fingerprint": fingerprint,
        "service": service,
        "severity": _normalize_severity(labels.get("severity") or payload.get("severity")),
        "alert_name": alert_name,
        "starts_at": _starts_at(
            first.get("startsAt"),
            payload.get("startsAt"),
            payload.get("starts_at"),
        ),
        "ends_at": _ends_at(first.get("endsAt") or payload.get("endsAt")),
        "labels": labels,
        "annotations": annotations,
    }


def _from_datadog(payload: dict[str, Any]) -> dict[str, Any]:
    tags = _list(payload.get("tags"))
    tag_map = _tags_to_labels(tags)
    service = _string(
        payload.get("service") or tag_map.get("service") or tag_map.get("kube_service"),
        "unknown",
    )
    alert_name = _string(
        payload.get("alert_title") or payload.get("title") or payload.get("alert_name"),
        "DatadogAlert",
    )
    fingerprint = _string(
        payload.get("fingerprint") or payload.get("dedup_key") or payload.get("alert_id"),
        f"datadog:{service}:{alert_name}",
    )
    annotations = {
        "message": payload.get("message"),
        "url": payload.get("url") or payload.get("link"),
        "status": payload.get("alert_status") or payload.get("status"),
    }
    return {
        "fingerprint": fingerprint,
        "service": service,
        "severity": _normalize_severity(
            payload.get("severity") or payload.get("alert_priority") or payload.get("priority")
        ),
        "alert_name": alert_name,
        "starts_at": _starts_at(
            payload.get("starts_at"),
            payload.get("date"),
            payload.get("last_updated"),
        ),
        "labels": tag_map,
        "annotations": {k: v for k, v in annotations.items() if v is not None},
    }


def _from_custom(payload: dict[str, Any]) -> dict[str, Any]:
    labels = _dict(payload.get("labels"))
    annotations = _dict(payload.get("annotations"))
    service = _string(
        payload.get("service") or labels.get("service") or labels.get("app"), "unknown"
    )
    alert_name = _string(
        payload.get("alert_name")
        or payload.get("alertname")
        or payload.get("title")
        or payload.get("name"),
        "CustomAlert",
    )
    return {
        "fingerprint": _string(
            payload.get("fingerprint") or payload.get("dedup_key") or payload.get("id"),
            f"custom:{service}:{alert_name}",
        ),
        "service": service,
        "severity": _normalize_severity(payload.get("severity") or labels.get("severity")),
        "alert_name": alert_name,
        "starts_at": _starts_at(
            payload.get("starts_at"),
            payload.get("timestamp"),
            payload.get("created_at"),
        ),
        "ends_at": _ends_at(payload.get("ends_at")),
        "labels": labels,
        "annotations": annotations,
    }


def _tags_to_labels(tags: list[Any]) -> dict[str, Any]:
    labels: dict[str, Any] = {}
    for item in tags:
        text = str(item)
        if ":" in text:
            key, value = text.split(":", 1)
            labels[key.replace(".", "_")] = value
        elif text:
            labels[text] = True
    return labels
