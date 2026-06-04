from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from apps.api.schemas.alerts import AlertCreateRequest
from apps.api.schemas.common import Severity
from apps.api.schemas.incidents import DiagnoseRequest


def test_alert_create_request_accepts_mock_payload() -> None:
    request = AlertCreateRequest(
        source="mock",
        fingerprint=" fp-1 ",
        service="api",
        severity=Severity.P2,
        alert_name="HighLatency",
        starts_at=datetime(2026, 6, 1, tzinfo=UTC),
    )
    assert request.fingerprint == "fp-1"
    assert request.labels == {}


def test_alert_create_request_rejects_blank_service() -> None:
    with pytest.raises(ValidationError):
        AlertCreateRequest(
            source="mock",
            fingerprint="fp-1",
            service=" ",
            severity="P2",
            alert_name="HighLatency",
            starts_at=datetime(2026, 6, 1, tzinfo=UTC),
        )


def test_diagnose_request_defaults_to_non_force() -> None:
    request = DiagnoseRequest()
    assert request.force is False
    assert request.reason is None


def test_alert_create_request_rejects_ends_at_before_starts_at() -> None:
    with pytest.raises(ValidationError, match="ends_at must be after starts_at"):
        AlertCreateRequest(
            source="mock",
            fingerprint="fp-1",
            service="api",
            severity="P2",
            alert_name="HighLatency",
            starts_at=datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
            ends_at=datetime(2026, 6, 1, 10, 0, tzinfo=UTC),
        )


def test_alert_create_request_accepts_ends_at_after_starts_at() -> None:
    request = AlertCreateRequest(
        source="mock",
        fingerprint="fp-1",
        service="api",
        severity="P2",
        alert_name="HighLatency",
        starts_at=datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
        ends_at=datetime(2026, 6, 1, 14, 0, tzinfo=UTC),
    )
    assert request.ends_at is not None
    assert request.ends_at > request.starts_at


def test_alertmanager_zero_ends_at_is_treated_as_open() -> None:
    request = AlertCreateRequest(
        receiver="sre",
        commonLabels={
            "alertname": "High5xxAfterDeploy",
            "service": "checkout",
            "severity": "critical",
        },
        alerts=[
            {
                "fingerprint": "am-zero-end",
                "startsAt": "2026-06-01T00:00:00Z",
                "endsAt": "0001-01-01T00:00:00Z",
            }
        ],
    )

    assert request.source == "alertmanager"
    assert request.ends_at is None


def test_alert_create_request_accepts_pagerduty_webhook_payload() -> None:
    request = AlertCreateRequest(
        event={
            "id": "evt_1",
            "event_type": "incident.triggered",
            "occurred_at": "2026-06-01T00:01:00Z",
            "data": {
                "id": "pd_inc_1",
                "summary": "Checkout database pool exhausted",
                "urgency": "high",
                "service": {"summary": "checkout"},
                "created_at": "2026-06-01T00:00:00Z",
            },
        }
    )

    assert request.source == "pagerduty"
    assert request.fingerprint == "pd_inc_1"
    assert request.service == "checkout"
    assert request.severity == Severity.P2
    assert request.raw_payload["event"]["id"] == "evt_1"


def test_alert_create_request_accepts_datadog_metric_alert_payload() -> None:
    request = AlertCreateRequest(
        alert_id="12345",
        title="Redis cache avalanche",
        tags=["service:checkout", "env:demo"],
        severity="warning",
        date="2026-06-01T00:00:00Z",
        message="cache miss rate is elevated",
    )

    assert request.source == "datadog"
    assert request.fingerprint == "12345"
    assert request.service == "checkout"
    assert request.labels["env"] == "demo"
    assert request.severity == Severity.P2


def test_mock_alert_missing_required_unified_fields_still_rejects() -> None:
    with pytest.raises(ValidationError):
        AlertCreateRequest(
            source="mock",
            fingerprint="fp-1",
            severity="P2",
            alert_name="HighLatency",
            starts_at=datetime(2026, 6, 1, tzinfo=UTC),
        )
