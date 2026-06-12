"""Integration tests for Config Publish / Rollback / Revoke API (PR 5.3)."""

from __future__ import annotations

from datetime import datetime, timezone

from packages.common.ids import new_id
from packages.common.time import utc_now
from packages.db.models import EffectiveConfigVersion


def _create_published_config(
    db,
    *,
    version_id: str | None = None,
    version_number: int = 1,
    config_snapshot: dict | None = None,
    published_by: str = "operator-key-1",
    status: str = "published",
    stale_after_days: int = 30,
) -> EffectiveConfigVersion:
    """Helper to create an EffectiveConfigVersion."""
    ecv = EffectiveConfigVersion(
        version_id=version_id or new_id("ecv_"),
        version_number=version_number,
        status=status,
        config_snapshot=config_snapshot
        or {"prometheus_url": "http://prom:9090"},
        published_at=utc_now(),
        published_by=published_by,
        stale_after_days=stale_after_days,
        stale_warning_at=utc_now(),
    )
    db.add(ecv)
    db.flush()
    return ecv


# ---------------------------------------------------------------------------
# GET /api/config/current
# ---------------------------------------------------------------------------


def test_get_config_current_no_config(client, db_session):
    """Returns status=none when no config is published."""
    response = client.get("/api/config/current")
    assert response.status_code == 200, response.json()
    data = response.json()
    assert data["status"] == "none"
    assert data["version_id"] is None


def test_get_config_current_with_config(client, db_session):
    """Returns the current published config."""
    ecv = _create_published_config(
        db_session,
        config_snapshot={
            "prometheus_url": "http://prom:9090",
            "loki_url": "http://loki:3100",
        },
    )

    response = client.get("/api/config/current")
    assert response.status_code == 200, response.json()
    data = response.json()
    assert data["version_id"] == ecv.version_id
    assert data["version_number"] == 1
    assert data["status"] == "published"
    assert data["config_snapshot"]["prometheus_url"] == "http://prom:9090"


# ---------------------------------------------------------------------------
# GET /api/config/versions
# ---------------------------------------------------------------------------


def test_get_config_versions_empty(client, db_session):
    """Returns empty list when no versions exist."""
    response = client.get("/api/config/versions")
    assert response.status_code == 200, response.json()
    data = response.json()
    assert data["versions"] == []
    assert data["total"] == 0


def test_get_config_versions_with_data(client, db_session):
    """Returns list of config versions."""
    _create_published_config(db_session, version_number=1)
    _create_published_config(
        db_session,
        version_number=2,
        config_snapshot={"prometheus_url": "http://prom2:9090"},
        published_by="operator-key-2",
        status="superseded",
    )

    response = client.get("/api/config/versions?limit=10")
    assert response.status_code == 200, response.json()
    data = response.json()
    assert data["total"] == 2
    assert data["versions"][0]["version_number"] == 2


# ---------------------------------------------------------------------------
# POST /api/config/publish
# ---------------------------------------------------------------------------


def test_config_publish_success(client, db_session):
    """Successfully publishes a new config version."""
    response = client.post(
        "/api/config/publish",
        json={
            "config_snapshot": {
                "prometheus_url": "http://prom:9090",
                "loki_url": "http://loki:3100",
            },
            "published_by": "operator-key-1",
            "stale_after_days": 30,
        },
    )
    assert response.status_code == 201, response.json()
    data = response.json()
    assert data["version_number"] == 1
    assert data["status"] == "published"
    assert data["version_id"].startswith("ecv_")

    # Verify in DB.
    ecv = db_session.query(EffectiveConfigVersion).filter_by(
        version_id=data["version_id"]
    ).first()
    assert ecv is not None
    assert ecv.status == "published"
    assert ecv.config_snapshot["prometheus_url"] == "http://prom:9090"


def test_config_publish_supersedes_previous(client, db_session):
    """Publishing a new version supersedes the previous one."""
    _create_published_config(db_session, version_number=1)

    response = client.post(
        "/api/config/publish",
        json={
            "config_snapshot": {"prometheus_url": "http://prom2:9090"},
            "published_by": "operator-key-2",
        },
    )
    assert response.status_code == 201, response.json()
    data = response.json()
    assert data["version_number"] == 2

    # Previous version should be superseded.
    versions = (
        db_session.query(EffectiveConfigVersion)
        .order_by(EffectiveConfigVersion.version_number)
        .all()
    )
    assert len(versions) == 2
    assert versions[0].status == "superseded"
    assert versions[1].status == "published"


def test_config_publish_writes_audit_log(client, db_session):
    """Publishing writes an audit log entry."""
    from packages.db.models import AuditLog

    response = client.post(
        "/api/config/publish",
        json={
            "config_snapshot": {"prometheus_url": "http://prom:9090"},
            "published_by": "operator-key-audit",
        },
    )
    assert response.status_code == 201, response.json()

    audit_entries = (
        db_session.query(AuditLog)
        .filter(AuditLog.action == "config.publish")
        .all()
    )
    assert len(audit_entries) >= 1


# ---------------------------------------------------------------------------
# POST /api/config/rollback
# ---------------------------------------------------------------------------


def test_config_rollback_success(client, db_session):
    """Successfully rolls back a published config."""
    _create_published_config(
        db_session, version_number=1, status="superseded"
    )
    ecv2 = _create_published_config(
        db_session, version_number=2, status="published"
    )

    response = client.post(
        "/api/config/rollback",
        json={
            "version_id": ecv2.version_id,
            "rolled_back_by": "operator-key-1",
        },
    )
    assert response.status_code == 200, response.json()
    data = response.json()
    assert data["status"] == "published"

    # ecv2 should be rolled_back.
    db_session.refresh(ecv2)
    assert ecv2.status == "rolled_back"


def test_config_rollback_not_found(client, db_session):
    """Rollback of non-existent version returns 400."""
    response = client.post(
        "/api/config/rollback",
        json={"version_id": "ecv_nonexistent"},
    )
    assert response.status_code == 400, response.json()


# ---------------------------------------------------------------------------
# POST /api/config/revoke
# ---------------------------------------------------------------------------


def test_config_revoke_success(client, db_session):
    """Successfully revokes a published config."""
    ecv = _create_published_config(db_session, status="published")

    response = client.post(
        "/api/config/revoke",
        json={
            "version_id": ecv.version_id,
            "revoked_by": "operator-key-1",
            "reason": "Misconfigured backend URL",
        },
    )
    assert response.status_code == 200, response.json()
    data = response.json()
    assert data["status"] == "revoked"

    # Verify in DB.
    db_session.refresh(ecv)
    assert ecv.status == "revoked"
    assert ecv.revoked_at is not None


def test_config_revoke_not_found(client, db_session):
    """Revoke of non-existent version returns 400."""
    response = client.post(
        "/api/config/revoke",
        json={"version_id": "ecv_nonexistent"},
    )
    assert response.status_code == 400, response.json()


def test_config_revoke_writes_audit_log(client, db_session):
    """Revoking writes an audit log entry."""
    from packages.db.models import AuditLog

    ecv = _create_published_config(db_session, status="published")

    response = client.post(
        "/api/config/revoke",
        json={
            "version_id": ecv.version_id,
            "revoked_by": "operator-key-revoke",
            "reason": "Test revoke",
        },
    )
    assert response.status_code == 200, response.json()

    audit_entries = (
        db_session.query(AuditLog)
        .filter(AuditLog.action == "config.revoke")
        .all()
    )
    assert len(audit_entries) >= 1
