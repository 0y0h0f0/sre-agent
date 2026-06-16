"""Integration tests for Alertmanager Poll Task (PR 4.7)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from packages.db.models import AlertPollCursor

# ---------------------------------------------------------------------------
# _from_alertmanager_single_alert
# ---------------------------------------------------------------------------


def test_from_alertmanager_single_alert_parse():
    """Parses a single Alertmanager alert from the API response format."""
    from apps.api.schemas.alerts import _from_alertmanager_single_alert

    alert = {
        "fingerprint": "abc123def456",
        "startsAt": "2026-06-12T10:00:00Z",
        "endsAt": "2026-06-12T10:05:00Z",
        "labels": {
            "alertname": "HighLatency",
            "service": "checkout",
            "severity": "critical",
            "namespace": "production",
        },
        "annotations": {
            "summary": "Checkout service p99 latency > 500ms",
        },
    }

    result = _from_alertmanager_single_alert(alert)

    assert result["fingerprint"] == "abc123def456"
    assert result["service"] == "checkout"
    assert result["alert_name"] == "HighLatency"
    assert result["severity"] == "P1"  # critical → P1
    assert result["labels"]["namespace"] == "production"
    assert result["ingestion_metadata"]["ingest_mode"] == "poll"
    assert result["raw_labels"]["alertname"] == "HighLatency"


def test_from_alertmanager_single_alert_no_fingerprint():
    """Derives fingerprint from service + alertname when not present."""
    from apps.api.schemas.alerts import _from_alertmanager_single_alert

    alert = {
        "startsAt": "2026-06-12T10:00:00Z",
        "labels": {
            "alertname": "CPUThrottle",
            "service": "payment-gateway",
            "severity": "warning",
        },
        "annotations": {},
    }

    result = _from_alertmanager_single_alert(alert)
    assert result["fingerprint"] == "alertmanager:payment-gateway:CPUThrottle"
    assert result["service"] == "payment-gateway"


def test_from_alertmanager_single_alert_raw_labels_preserved():
    """raw_labels are preserved exactly as received, not modified."""
    from apps.api.schemas.alerts import _from_alertmanager_single_alert

    alert = {
        "labels": {
            "alertname": "TestAlert",
            "service": "test-svc",
            "custom_label": "original_value",
            "severity": "p2",
        },
        "annotations": {},
        "startsAt": "2026-06-12T10:00:00Z",
    }

    result = _from_alertmanager_single_alert(alert)
    # raw_labels must be a copy, not the same reference.
    assert result["raw_labels"]["custom_label"] == "original_value"
    assert result["raw_labels"]["service"] == "test-svc"
    # The poll marker is NOT in raw_labels.
    assert "ingest_mode" not in result["raw_labels"]


def test_poll_and_webhook_same_fingerprint():
    """Poll and webhook paths produce the same fingerprint for the same alert."""
    from apps.api.schemas.alerts import (
        _from_alertmanager,
        _from_alertmanager_single_alert,
    )

    # Simulate webhook payload (with alerts array, commonLabels, etc.).
    webhook_payload = {
        "groupLabels": {"alertname": "HighErrorRate"},
        "commonLabels": {"service": "checkout", "severity": "critical"},
        "commonAnnotations": {"summary": "Error rate spike"},
        "alerts": [
            {
                "fingerprint": "fp-test-001",
                "labels": {"service": "checkout", "alertname": "HighErrorRate"},
                "annotations": {"summary": "Error rate spike"},
                "startsAt": "2026-06-12T10:00:00Z",
            }
        ],
    }

    webhook_result = _from_alertmanager(webhook_payload)

    # Simulate polled alert from GET /api/v2/alerts.
    poll_alert = {
        "fingerprint": "fp-test-001",
        "labels": {"service": "checkout", "alertname": "HighErrorRate"},
        "annotations": {"summary": "Error rate spike"},
        "startsAt": "2026-06-12T10:00:00Z",
    }

    poll_result = _from_alertmanager_single_alert(poll_alert)

    # Both paths produce the same fingerprint.
    assert webhook_result["fingerprint"] == "fp-test-001"
    assert poll_result["fingerprint"] == "fp-test-001"
    assert webhook_result["fingerprint"] == poll_result["fingerprint"]
    assert poll_result["service"] == "checkout"
    assert webhook_result["service"] == "checkout"


# ---------------------------------------------------------------------------
# _build_filter_hash
# ---------------------------------------------------------------------------


def test_filter_hash_deterministic():
    """Same filters produce the same hash."""
    from apps.worker.tasks import _build_filter_hash
    from packages.discovery.matcher_parser import AlertPollFilters

    f1 = AlertPollFilters(receiver="ops", namespace_allowlist=["prod"])
    f2 = AlertPollFilters(receiver="ops", namespace_allowlist=["prod"])

    assert _build_filter_hash(f1) == _build_filter_hash(f2)


def test_filter_hash_different_scopes():
    """Different filters produce different hashes."""
    from apps.worker.tasks import _build_filter_hash
    from packages.discovery.matcher_parser import AlertPollFilters

    f1 = AlertPollFilters(receiver="ops")
    f2 = AlertPollFilters(receiver="dev")

    assert _build_filter_hash(f1) != _build_filter_hash(f2)


def test_filter_hash_order_independent():
    """Hash is independent of allowlist ordering."""
    from apps.worker.tasks import _build_filter_hash
    from packages.discovery.matcher_parser import AlertPollFilters

    f1 = AlertPollFilters(
        namespace_allowlist=["prod", "staging"],
        service_allowlist=["svc-a", "svc-b"],
    )
    f2 = AlertPollFilters(
        namespace_allowlist=["staging", "prod"],
        service_allowlist=["svc-b", "svc-a"],
    )

    assert _build_filter_hash(f1) == _build_filter_hash(f2)


# ---------------------------------------------------------------------------
# _get_poll_filters
# ---------------------------------------------------------------------------


def test_get_poll_filters_from_settings():
    """Builds AlertPollFilters from settings with receiver."""
    mock_settings = MagicMock()
    mock_settings.alert_poll_receiver_filter = "ops-team"
    mock_settings.alert_poll_namespace_allowlist = "prod, staging"
    mock_settings.alert_poll_service_allowlist = ""
    mock_settings.alert_poll_filter_matchers = ""

    from apps.worker.tasks import _get_poll_filters

    filters = _get_poll_filters(mock_settings)
    assert filters.receiver == "ops-team"
    assert filters.namespace_allowlist == ["prod", "staging"]
    assert filters.service_allowlist == []


def test_get_poll_filters_empty_receiver():
    """Empty receiver string becomes None."""
    mock_settings = MagicMock()
    mock_settings.alert_poll_receiver_filter = "   "
    mock_settings.alert_poll_namespace_allowlist = "prod"
    mock_settings.alert_poll_service_allowlist = ""
    mock_settings.alert_poll_filter_matchers = ""

    from apps.worker.tasks import _get_poll_filters

    filters = _get_poll_filters(mock_settings)
    assert filters.receiver is None
    assert filters.namespace_allowlist == ["prod"]


# ---------------------------------------------------------------------------
# Poll task logic (mocked)
# ---------------------------------------------------------------------------


def test_poll_task_skips_when_source_not_poll():
    """Poll task returns skipped when alert_source is not poll/both."""
    mock_settings = MagicMock()
    mock_settings.alert_source = "webhook"
    mock_settings.redis_url = "redis://localhost:6379/0"


    with patch("apps.worker.tasks.get_settings", return_value=mock_settings), \
         patch("redis.Redis.from_url", return_value=MagicMock()):
        from apps.worker.tasks import poll_alertmanager
        result = poll_alertmanager()
        assert result["status"] == "skipped"


def test_poll_task_skips_when_no_valid_scope():
    """Poll task returns skipped when scope is invalid."""
    mock_settings = MagicMock()
    mock_settings.alert_source = "poll"
    mock_settings.alert_poll_receiver_filter = ""
    mock_settings.alert_poll_namespace_allowlist = ""
    mock_settings.alert_poll_service_allowlist = ""
    mock_settings.alert_poll_filter_matchers = ""
    mock_settings.redis_url = "redis://localhost:6379/0"


    with patch("apps.worker.tasks.get_settings", return_value=mock_settings), \
         patch("redis.Redis.from_url", return_value=MagicMock()):
        from apps.worker.tasks import poll_alertmanager
        result = poll_alertmanager()
        assert result["status"] == "skipped"
        assert "no valid poll scope" in result["reason"]


# ---------------------------------------------------------------------------
# PollCursorRepository
# ---------------------------------------------------------------------------


def test_poll_cursor_already_seen_resets_missing_rounds(db_session):
    """already_seen_active resets missing_rounds on existing cursor."""
    from packages.common.time import utc_now
    from packages.db.repositories.poll_cursor import PollCursorRepository

    now = utc_now()
    cursor = AlertPollCursor(
        filter_hash="hash-001",
        fingerprint="fp-001",
        incident_id="inc-001",
        last_seen_at=now,
        first_seen_at=now,
        missing_rounds=5,
    )
    db_session.add(cursor)
    db_session.commit()

    repo = PollCursorRepository(db_session)
    result = repo.already_seen_active("fp-001", "hash-001")

    assert result is True
    assert cursor.missing_rounds == 0


def test_poll_cursor_already_seen_new_fingerprint(db_session):
    """already_seen_active returns False for unknown fingerprint."""
    from packages.db.repositories.poll_cursor import PollCursorRepository

    repo = PollCursorRepository(db_session)
    result = repo.already_seen_active("fp-new", "hash-001")
    assert result is False


def test_poll_cursor_mark_missing_increments(db_session):
    """mark_missing increments missing_rounds counter."""
    from packages.common.time import utc_now
    from packages.db.repositories.poll_cursor import PollCursorRepository

    now = utc_now()
    cursor = AlertPollCursor(
        filter_hash="hash-001",
        fingerprint="fp-002",
        incident_id="inc-002",
        last_seen_at=now,
        first_seen_at=now,
        missing_rounds=0,
    )
    db_session.add(cursor)
    db_session.commit()

    repo = PollCursorRepository(db_session)
    repo.mark_missing("fp-002", "hash-001")

    assert cursor.missing_rounds == 1


# ---------------------------------------------------------------------------
# Resolved inference
# ---------------------------------------------------------------------------


def test_resolved_inference_truncated_blocks_resolution():
    """Truncated results block resolved inference."""
    from packages.discovery.resolved_inference import (
        infer_resolved_from_missing_fingerprints,
    )

    mock_repo = MagicMock()
    mock_repo.get_filter_hashes_for_fingerprint.return_value = ["hash-001"]
    mock_repo.get_missing_rounds.return_value = 10
    mock_repo.get_first_seen_at.return_value = None

    decision = infer_resolved_from_missing_fingerprints(
        fingerprint="fp-001",
        all_active_filter_hashes=["hash-001"],
        cursor_repo=mock_repo,
        results_truncated=True,
    )
    assert decision.is_resolved is False
    assert "truncated" in decision.reason


def test_resolved_inference_grace_period():
    """Fingerprint within grace period is not eligible for resolution."""
    import time

    from packages.discovery.resolved_inference import (
        infer_resolved_from_missing_fingerprints,
    )

    mock_repo = MagicMock()
    mock_repo.get_filter_hashes_for_fingerprint.return_value = ["hash-001"]
    mock_repo.get_missing_rounds.return_value = 5
    mock_repo.get_first_seen_at.return_value = time.time()  # just now

    decision = infer_resolved_from_missing_fingerprints(
        fingerprint="fp-grace",
        all_active_filter_hashes=["hash-001"],
        cursor_repo=mock_repo,
        grace_rounds=3,
        resolved_rounds=3,
        poll_interval_seconds=30,
    )
    assert decision.is_resolved is False
    assert "grace" in decision.reason


def test_resolved_inference_all_missing_resolves():
    """Fingerprint missing across all filter hashes resolves."""
    from packages.discovery.resolved_inference import (
        infer_resolved_from_missing_fingerprints,
    )

    mock_repo = MagicMock()
    mock_repo.get_filter_hashes_for_fingerprint.return_value = ["hash-001"]
    mock_repo.get_missing_rounds.return_value = 5
    mock_repo.get_first_seen_at.return_value = None  # past grace period

    decision = infer_resolved_from_missing_fingerprints(
        fingerprint="fp-to-resolve",
        all_active_filter_hashes=["hash-001"],
        cursor_repo=mock_repo,
        grace_rounds=3,
        resolved_rounds=3,
    )
    assert decision.is_resolved is True
    assert "all_filter_hashes_missing" in decision.reason


def test_resolved_inference_insufficient_missing():
    """Not enough missing rounds returns not-resolved."""
    from packages.discovery.resolved_inference import (
        infer_resolved_from_missing_fingerprints,
    )

    mock_repo = MagicMock()
    mock_repo.get_filter_hashes_for_fingerprint.return_value = ["hash-001"]
    mock_repo.get_missing_rounds.return_value = 1  # only 1 round missing
    mock_repo.get_first_seen_at.return_value = None

    decision = infer_resolved_from_missing_fingerprints(
        fingerprint="fp-still-alive",
        all_active_filter_hashes=["hash-001"],
        cursor_repo=mock_repo,
        grace_rounds=3,
        resolved_rounds=3,
    )
    assert decision.is_resolved is False
    assert "insufficient_missing" in decision.reason
