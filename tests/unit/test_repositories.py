from __future__ import annotations

from datetime import UTC, datetime

from apps.api.schemas.alerts import AlertCreateRequest
from packages.db.repositories.agent_runs import AgentRunRepository
from packages.db.repositories.incidents import IncidentRepository


def test_incident_repository_filters_open_fingerprints(db_session) -> None:
    payload = AlertCreateRequest(
        source="mock",
        fingerprint="fp-1",
        service="api",
        severity="P2",
        alert_name="HighLatency",
        starts_at=datetime(2026, 6, 1, tzinfo=UTC),
    )
    repo = IncidentRepository(db_session)
    incident = repo.create("inc_1", payload)
    db_session.commit()

    assert repo.get_open_by_fingerprint("fp-1") == incident
    incident.status = "resolved"
    db_session.commit()
    assert repo.get_open_by_fingerprint("fp-1") is None


def test_agent_run_repository_tracks_latest_and_active(db_session) -> None:
    payload = AlertCreateRequest(
        source="mock",
        fingerprint="fp-2",
        service="api",
        severity="P3",
        alert_name="CacheAvalanche",
        starts_at=datetime(2026, 6, 1, tzinfo=UTC),
    )
    IncidentRepository(db_session).create("inc_2", payload)
    repo = AgentRunRepository(db_session)
    repo.create("run_1", "inc_2", model_name="fake")
    db_session.commit()

    assert repo.get_latest_for_incident("inc_2").agent_run_id == "run_1"
    assert repo.get_active_for_incident("inc_2").agent_run_id == "run_1"
    repo.set_task_id("run_1", "task-1")
    db_session.commit()
    assert repo.get_by_public_id("run_1").celery_task_id == "task-1"


def test_set_task_id_raises_for_missing_run(db_session) -> None:
    import pytest

    repo = AgentRunRepository(db_session)
    with pytest.raises(ValueError, match="agent run no_such_run not found"):
        repo.set_task_id("no_such_run", "task-99")


def test_mark_enqueue_failed_sets_status_and_error(db_session) -> None:
    payload = AlertCreateRequest(
        source="mock",
        fingerprint="fp-enq-fail",
        service="api",
        severity="P3",
        alert_name="HighLatency",
        starts_at=datetime(2026, 6, 1, tzinfo=UTC),
    )
    IncidentRepository(db_session).create("inc_enq", payload)
    repo = AgentRunRepository(db_session)
    repo.create("run_enq", "inc_enq", model_name="fake")
    db_session.commit()

    repo.mark_enqueue_failed("run_enq", "broker unreachable")
    db_session.commit()

    run = repo.get_by_public_id("run_enq")
    assert run.status == "failed"
    assert run.error_code == "CELERY_ENQUEUE_FAILED"
    assert run.error_message == "broker unreachable"
    assert run.finished_at is not None


def test_mark_succeeded_calculates_duration(db_session) -> None:
    from datetime import timedelta

    from packages.common.time import utc_now

    payload = AlertCreateRequest(
        source="mock",
        fingerprint="fp-dur",
        service="api",
        severity="P3",
        alert_name="HighLatency",
        starts_at=datetime(2026, 6, 1, tzinfo=UTC),
    )
    IncidentRepository(db_session).create("inc_dur", payload)
    repo = AgentRunRepository(db_session)
    run = repo.create("run_dur", "inc_dur", model_name="fake")
    db_session.commit()

    run.started_at = utc_now() - timedelta(seconds=3)
    repo.mark_succeeded(run, {"key": "value"})
    db_session.commit()

    refreshed = repo.get_by_public_id("run_dur")
    assert refreshed.status == "succeeded"
    assert refreshed.duration_ms is not None
    assert 2900 <= refreshed.duration_ms <= 3100
    assert refreshed.state == {"key": "value"}


def test_mark_failed_sets_status_and_error(db_session) -> None:
    from datetime import timedelta

    from packages.common.time import utc_now

    payload = AlertCreateRequest(
        source="mock",
        fingerprint="fp-fail",
        service="api",
        severity="P3",
        alert_name="HighLatency",
        starts_at=datetime(2026, 6, 1, tzinfo=UTC),
    )
    IncidentRepository(db_session).create("inc_fail", payload)
    repo = AgentRunRepository(db_session)
    run = repo.create("run_fail", "inc_fail", model_name="fake")
    db_session.commit()

    run.started_at = utc_now() - timedelta(seconds=1)
    repo.mark_failed("run_fail", "TOOL_ERROR", "Prometheus unreachable")
    db_session.commit()

    refreshed = repo.get_by_public_id("run_fail")
    assert refreshed.status == "failed"
    assert refreshed.error_code == "TOOL_ERROR"
    assert refreshed.error_message == "Prometheus unreachable"
    assert refreshed.finished_at is not None
    assert refreshed.duration_ms is not None


def test_mark_failed_raises_for_missing_run(db_session) -> None:
    import pytest

    repo = AgentRunRepository(db_session)
    with pytest.raises(ValueError, match="agent run no_such not found"):
        repo.mark_failed("no_such", "ERR", "message")
