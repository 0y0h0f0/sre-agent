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
