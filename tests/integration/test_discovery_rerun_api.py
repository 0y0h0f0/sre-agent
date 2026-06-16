"""Integration tests for Discovery Rerun API (PR 5.2).

Tests POST /api/discovery/rerun with scope validation and Redis lock.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from packages.db.models import DiscoveryRun

# ---------------------------------------------------------------------------
# POST /api/discovery/rerun — success path
# ---------------------------------------------------------------------------


def test_rerun_creates_run_and_returns_task_id(client, db_session, monkeypatch):
    """A successful rerun creates a DiscoveryRun and returns a task_id."""
    # Mock Redis to avoid needing a real Redis server in tests.
    mock_redis = MagicMock()
    mock_redis.ping.return_value = True
    mock_redis.set.return_value = True

    monkeypatch.setattr(
        "redis.Redis.from_url",
        MagicMock(return_value=mock_redis),
    )
    monkeypatch.setattr(
        "redis.Redis",
        MagicMock(return_value=mock_redis),
    )

    # Mock Celery enqueue to avoid needing a real broker.
    monkeypatch.setattr(
        "apps.worker.tasks.run_discovery_rerun.delay",
        MagicMock(return_value=MagicMock(id="mock-task-id-001")),
    )

    response = client.post(
        "/api/discovery/rerun",
        json={"triggered_by": "operator-key-1"},
    )
    assert response.status_code == 202, response.json()
    data = response.json()
    assert data["status"] == "enqueued"
    assert data["discovery_run_id"].startswith("dr_")
    assert data["task_id"] == "mock-task-id-001"

    # Verify the DiscoveryRun was created in the DB.
    run = db_session.query(DiscoveryRun).filter_by(
        discovery_run_id=data["discovery_run_id"]
    ).first()
    assert run is not None
    assert run.source == "manual_rerun"
    assert run.trigger_type == "manual"
    assert run.triggered_by == "operator-key-1"


def test_rerun_without_triggered_by(client, db_session, monkeypatch):
    """Rerun works without triggered_by field."""
    mock_redis = MagicMock()
    mock_redis.ping.return_value = True
    mock_redis.set.return_value = True

    monkeypatch.setattr("redis.Redis.from_url", MagicMock(return_value=mock_redis))
    monkeypatch.setattr("redis.Redis", MagicMock(return_value=mock_redis))
    monkeypatch.setattr(
        "apps.worker.tasks.run_discovery_rerun.delay",
        MagicMock(return_value=MagicMock(id="mock-task-id-002")),
    )

    response = client.post("/api/discovery/rerun")
    assert response.status_code == 202, response.json()
    data = response.json()
    assert data["status"] == "enqueued"
    assert data["discovery_run_id"].startswith("dr_")


# ---------------------------------------------------------------------------
# POST /api/discovery/rerun — Redis lock conflicts
# ---------------------------------------------------------------------------


def test_rerun_returns_locked_when_redis_lock_held(client, db_session, monkeypatch):
    """Returns locked status when another discovery run holds the lock."""
    mock_redis = MagicMock()
    mock_redis.ping.return_value = True

    # Patch RedisLock.acquire to return False (lock held by another process).
    monkeypatch.setattr(
        "packages.common.redis_lock.RedisLock.acquire",
        MagicMock(return_value=False),
    )
    monkeypatch.setattr("redis.Redis.from_url", MagicMock(return_value=mock_redis))
    monkeypatch.setattr("redis.Redis", MagicMock(return_value=mock_redis))

    response = client.post(
        "/api/discovery/rerun",
        json={"triggered_by": "operator-key-2"},
    )
    assert response.status_code == 202, response.json()
    data = response.json()
    assert data["status"] == "locked"
    assert "Another discovery run is in progress" in data["message"]


# ---------------------------------------------------------------------------
# POST /api/discovery/rerun — Redis unavailable (degraded)
# ---------------------------------------------------------------------------


def test_rerun_proceeds_when_redis_unavailable(client, db_session, monkeypatch):
    """Rerun proceeds without lock when Redis is unavailable."""
    mock_redis = MagicMock()
    mock_redis.ping.side_effect = Exception("Connection refused")

    monkeypatch.setattr("redis.Redis.from_url", MagicMock(return_value=mock_redis))
    monkeypatch.setattr("redis.Redis", MagicMock(return_value=mock_redis))
    monkeypatch.setattr(
        "apps.worker.tasks.run_discovery_rerun.delay",
        MagicMock(return_value=MagicMock(id="mock-task-id-003")),
    )

    response = client.post("/api/discovery/rerun")
    assert response.status_code == 202, response.json()
    data = response.json()
    assert data["status"] == "enqueued"


# ---------------------------------------------------------------------------
# POST /api/discovery/rerun — task enqueue failure
# ---------------------------------------------------------------------------


def test_rerun_returns_500_on_enqueue_failure(client, db_session, monkeypatch):
    """Returns 500 when Celery task enqueue fails."""
    mock_redis = MagicMock()
    mock_redis.ping.return_value = True
    mock_redis.set.return_value = True

    monkeypatch.setattr("redis.Redis.from_url", MagicMock(return_value=mock_redis))
    monkeypatch.setattr("redis.Redis", MagicMock(return_value=mock_redis))
    monkeypatch.setattr(
        "apps.worker.tasks.run_discovery_rerun.delay",
        MagicMock(side_effect=Exception("Broker connection failed")),
    )

    response = client.post("/api/discovery/rerun")
    assert response.status_code == 500, response.json()


# ---------------------------------------------------------------------------
# POST /api/discovery/rerun — audit log
# ---------------------------------------------------------------------------


def test_rerun_writes_audit_log(client, db_session, monkeypatch):
    """A successful rerun writes an audit log entry."""
    from packages.db.models import AuditLog

    mock_redis = MagicMock()
    mock_redis.ping.return_value = True
    mock_redis.set.return_value = True

    monkeypatch.setattr("redis.Redis.from_url", MagicMock(return_value=mock_redis))
    monkeypatch.setattr("redis.Redis", MagicMock(return_value=mock_redis))
    monkeypatch.setattr(
        "apps.worker.tasks.run_discovery_rerun.delay",
        MagicMock(return_value=MagicMock(id="mock-task-id-audit")),
    )

    response = client.post(
        "/api/discovery/rerun",
        json={"triggered_by": "operator-key-audit"},
    )
    assert response.status_code == 202, response.json()

    # Verify audit log was written.
    audit_entries = (
        db_session.query(AuditLog)
        .filter(AuditLog.action == "discovery.rerun_requested")
        .all()
    )
    assert len(audit_entries) >= 1
    found = any(
        "operator-key-audit" in (entry.actor or "")
        for entry in audit_entries
    )
    assert found, "Audit log should contain the actor"
