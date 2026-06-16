"""Integration tests for Override API (PR 5.4)."""

from __future__ import annotations

from datetime import timedelta

from packages.common.time import utc_now
from packages.db.models import DiscoveryOverride

# ---------------------------------------------------------------------------
# GET /api/config/overrides
# ---------------------------------------------------------------------------


def test_get_overrides_empty(client, db_session):
    """Returns empty list when no overrides exist."""
    response = client.get("/api/config/overrides")
    assert response.status_code == 200, response.json()
    data = response.json()
    assert data["overrides"] == []
    assert data["total"] == 0


def test_get_overrides_active_only(client, db_session):
    """Only returns active overrides (not expired, not revoked)."""
    now = utc_now()
    # Active override.
    active = DiscoveryOverride(
        override_id="dov_active001",
        backend_type="prometheus",
        override_json={"url": "http://prom2:9090"},
        reason="Test active",
        expires_at=now + timedelta(days=7),
    )
    # Expired override.
    expired = DiscoveryOverride(
        override_id="dov_expired001",
        backend_type="loki",
        override_json={"url": "http://loki-old:3100"},
        reason="Test expired",
        expires_at=now - timedelta(days=1),
    )
    # Revoked override.
    revoked = DiscoveryOverride(
        override_id="dov_revoked001",
        backend_type="jaeger",
        override_json={"url": "http://jaeger:16686"},
        reason="Test revoked",
        expires_at=now + timedelta(days=7),
        revoked_at=now,
    )
    db_session.add_all([active, expired, revoked])
    db_session.flush()

    response = client.get("/api/config/overrides")
    assert response.status_code == 200, response.json()
    data = response.json()
    assert data["total"] == 1
    assert data["overrides"][0]["override_id"] == "dov_active001"


# ---------------------------------------------------------------------------
# POST /api/config/overrides
# ---------------------------------------------------------------------------


def test_override_create_success(client, db_session):
    """Successfully creates an override."""
    response = client.post(
        "/api/config/overrides",
        json={
            "backend_type": "prometheus",
            "override_json": {"url": "http://prom-test:9090"},
            "reason": "Testing override creation",
            "created_by_key_id": "operator-key-1",
        },
    )
    assert response.status_code == 201, response.json()
    data = response.json()
    assert data["override_id"].startswith("dov_")
    assert data["backend_type"] == "prometheus"
    assert data["is_active"] is True
    assert data["reason"] == "Testing override creation"
    assert data["expires_at"] is not None

    # Verify in DB.
    ov = db_session.query(DiscoveryOverride).filter_by(
        override_id=data["override_id"]
    ).first()
    assert ov is not None
    assert ov.backend_type == "prometheus"


def test_override_requires_reason(client, db_session):
    """Creating override without reason returns 422."""
    response = client.post(
        "/api/config/overrides",
        json={
            "backend_type": "prometheus",
            "override_json": {"url": "http://prom:9090"},
        },
    )
    assert response.status_code == 422, response.json()


def test_override_max_ttl_rejected(client, db_session):
    """Override with too-long TTL is rejected."""
    far_future = (utc_now() + timedelta(days=365)).isoformat()
    response = client.post(
        "/api/config/overrides",
        json={
            "backend_type": "prometheus",
            "override_json": {"url": "http://prom:9090"},
            "reason": "TTL too long",
            "expires_at": far_future,
        },
    )
    assert response.status_code == 400, response.json()
    assert "TTL exceeds maximum" in response.json()["error"]["message"]


def test_override_rejects_unsafe_url(client, db_session):
    """Override with unsafe backend URL is rejected."""
    response = client.post(
        "/api/config/overrides",
        json={
            "backend_type": "prometheus",
            "override_json": {"url": "file:///etc/passwd"},
            "reason": "Unsafe URL test",
        },
    )
    assert response.status_code == 400, response.json()
    assert "Unsafe" in response.json()["error"]["message"]


def test_override_rejects_localhost_url_in_production(client, db_session, test_settings):
    """Override URL validation must use production settings, not local defaults."""
    test_settings.app_env = "production"
    response = client.post(
        "/api/config/overrides",
        json={
            "backend_type": "prometheus",
            "override_json": {"url": "http://localhost:9090"},
            "reason": "Production localhost must be rejected",
        },
    )
    assert response.status_code == 400, response.json()
    assert "Unsafe" in response.json()["error"]["message"]


def test_override_rejects_secret_field(client, db_session):
    """Override with secret field is rejected."""
    response = client.post(
        "/api/config/overrides",
        json={
            "backend_type": "prometheus",
            "override_json": {"url": "http://prom:9090", "bearer_token": "secret123"},
            "reason": "Should be rejected",
        },
    )
    assert response.status_code == 400, response.json()
    assert "bearer_token" in response.json()["error"]["message"]


def test_override_rejects_executor_field(client, db_session):
    """Override with executor field is rejected."""
    response = client.post(
        "/api/config/overrides",
        json={
            "backend_type": "alertmanager",
            "override_json": {"executor_backend": "live"},
            "reason": "Should be rejected",
        },
    )
    assert response.status_code == 400, response.json()


def test_override_allows_explicit_expires_at(client, db_session):
    """Override with explicit expires_at within max TTL is accepted."""
    expires = (utc_now() + timedelta(days=14)).isoformat()
    response = client.post(
        "/api/config/overrides",
        json={
            "backend_type": "loki",
            "override_json": {"url": "http://loki:3100"},
            "reason": "Explicit expiry",
            "expires_at": expires,
        },
    )
    assert response.status_code == 201, response.json()
    data = response.json()
    assert data["is_active"] is True


# ---------------------------------------------------------------------------
# DELETE /api/config/overrides/{id}
# ---------------------------------------------------------------------------


def test_override_revoke_success(client, db_session):
    """Successfully revokes an active override."""
    now = utc_now()
    ov = DiscoveryOverride(
        override_id="dov_revoke_test",
        backend_type="prometheus",
        override_json={"url": "http://prom:9090"},
        reason="To be revoked",
        expires_at=now + timedelta(days=7),
    )
    db_session.add(ov)
    db_session.flush()

    response = client.request(
        "DELETE",
        "/api/config/overrides/dov_revoke_test",
        json={"reason": "No longer needed", "revoked_by": "operator-key-1"},
    )
    assert response.status_code == 200, response.json()
    data = response.json()
    assert data["status"] == "revoked"

    db_session.refresh(ov)
    assert ov.revoked_at is not None
    assert ov.revoke_reason == "No longer needed"


def test_override_revoke_not_found(client, db_session):
    """Revoking non-existent override returns 404."""
    response = client.request("DELETE", "/api/config/overrides/dov_nonexistent")
    assert response.status_code == 404, response.json()


def test_override_revoke_already_revoked(client, db_session):
    """Revoking already-revoked override returns 400."""
    now = utc_now()
    ov = DiscoveryOverride(
        override_id="dov_already_revoked",
        backend_type="prometheus",
        override_json={"url": "http://prom:9090"},
        reason="Already done",
        expires_at=now + timedelta(days=7),
        revoked_at=now,
    )
    db_session.add(ov)
    db_session.flush()

    response = client.request("DELETE", "/api/config/overrides/dov_already_revoked")
    assert response.status_code == 400, response.json()
    assert "already revoked" in response.json()["error"]["message"].lower()
