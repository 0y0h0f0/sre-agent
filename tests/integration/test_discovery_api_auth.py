"""Auth tests for discovery API — PR 5.2.

Verifies that discovery:read and discovery:write scopes are enforced
on discovery read and rerun endpoints respectively.
"""

from __future__ import annotations

import hashlib
import secrets
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from apps.api.dependencies import get_app_settings
from packages.common.settings import Settings, get_settings
from packages.db.base import Base
from packages.db.models import ApiKey


def _hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def _create_api_key(session: object, *, scopes: list[str] | None = None) -> str:
    """Create an API key in the given session and return the raw key."""
    raw_key = secrets.token_hex(32)
    key_hash = _hash_key(raw_key)
    key = ApiKey(
        key_id=f"apik_test_{secrets.token_hex(4)}",
        description="auth-test-key",
        key_hash=key_hash,
        scopes=scopes or [],
    )
    session.add(key)
    session.flush()
    return raw_key


@pytest.fixture()
def auth_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """TestClient with API key auth enabled against an in-memory SQLite DB.

    Patches packages.db.session.SessionLocal to use a test DB so the
    auth middleware can verify API keys stored in the same DB.
    """
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    monkeypatch.setenv("API_KEY_AUTH_ENABLED", "true")
    monkeypatch.setenv("LLM_PROVIDER", "fake")
    monkeypatch.setenv("EMBEDDING_PROVIDER", "fake")
    get_settings.cache_clear()

    from apps.api.main import create_app

    app = create_app()

    app.dependency_overrides[get_app_settings] = lambda: Settings(
        database_url="sqlite+pysqlite:///:memory:",
        api_key_auth_enabled=True,
        celery_task_always_eager=True,
        llm_provider="fake",
        embedding_provider="fake",
    )

    with (
        patch("apps.api.middleware.auth.SessionLocal", TestSession),
        patch("packages.db.session.SessionLocal", TestSession),
    ):
        with TestClient(app) as client:
            # Store the session factory on client for convenience
            client._test_session_factory = TestSession  # type: ignore[attr-defined]
            yield client
    app.dependency_overrides.clear()
    get_settings.cache_clear()


def _make_session(auth_client: TestClient):
    """Create a fresh session using the test DB."""
    factory = getattr(auth_client, "_test_session_factory", None)
    if factory is None:
        raise RuntimeError("Test session factory not available")
    return factory()


# ---------------------------------------------------------------------------
# Discovery:read scope tests
# ---------------------------------------------------------------------------


class TestDiscoveryReadAuth:
    def test_status_returns_401_without_auth_header(self, auth_client: TestClient):
        resp = auth_client.get("/api/discovery/status")
        assert resp.status_code == 401

    def test_status_returns_401_with_invalid_key(self, auth_client: TestClient):
        resp = auth_client.get(
            "/api/discovery/status",
            headers={"Authorization": "Bearer invalid-key-here"},
        )
        assert resp.status_code == 401

    def test_status_allows_discovery_read_scope(
        self, auth_client: TestClient
    ):
        with _make_session(auth_client) as session:
            raw = _create_api_key(session, scopes=["discovery:read"])
            session.commit()

        resp = auth_client.get(
            "/api/discovery/status",
            headers={"Authorization": f"Bearer {raw}"},
        )
        assert resp.status_code == 200

    def test_status_allows_discovery_write_scope(
        self, auth_client: TestClient
    ):
        with _make_session(auth_client) as session:
            raw = _create_api_key(session, scopes=["discovery:write"])
            session.commit()

        resp = auth_client.get(
            "/api/discovery/status",
            headers={"Authorization": f"Bearer {raw}"},
        )
        assert resp.status_code == 200

    def test_status_rejects_no_matching_scope(
        self, auth_client: TestClient
    ):
        with _make_session(auth_client) as session:
            raw = _create_api_key(session, scopes=["config:read"])
            session.commit()

        resp = auth_client.get(
            "/api/discovery/status",
            headers={"Authorization": f"Bearer {raw}"},
        )
        assert resp.status_code == 403

    def test_services_allows_discovery_read(
        self, auth_client: TestClient
    ):
        with _make_session(auth_client) as session:
            raw = _create_api_key(session, scopes=["discovery:read"])
            session.commit()

        resp = auth_client.get(
            "/api/discovery/services",
            headers={"Authorization": f"Bearer {raw}"},
        )
        assert resp.status_code == 200

    def test_metrics_allows_discovery_read(
        self, auth_client: TestClient
    ):
        with _make_session(auth_client) as session:
            raw = _create_api_key(session, scopes=["discovery:read"])
            session.commit()

        resp = auth_client.get(
            "/api/discovery/metrics",
            headers={"Authorization": f"Bearer {raw}"},
        )
        assert resp.status_code == 200

    def test_topology_allows_discovery_read(
        self, auth_client: TestClient
    ):
        with _make_session(auth_client) as session:
            raw = _create_api_key(session, scopes=["discovery:read"])
            session.commit()

        resp = auth_client.get(
            "/api/discovery/topology",
            headers={"Authorization": f"Bearer {raw}"},
        )
        assert resp.status_code == 200

    def test_capabilities_allows_discovery_read(
        self, auth_client: TestClient
    ):
        with _make_session(auth_client) as session:
            raw = _create_api_key(session, scopes=["discovery:read"])
            session.commit()

        resp = auth_client.get(
            "/api/discovery/capabilities",
            headers={"Authorization": f"Bearer {raw}"},
        )
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Discovery:write scope tests (rerun)
# ---------------------------------------------------------------------------


class TestDiscoveryRerunAuth:
    def test_rerun_returns_401_without_auth(self, auth_client: TestClient):
        resp = auth_client.post("/api/discovery/rerun")
        assert resp.status_code == 401

    def test_rerun_rejects_discovery_read_only(
        self, auth_client: TestClient
    ):
        with _make_session(auth_client) as session:
            raw = _create_api_key(session, scopes=["discovery:read"])
            session.commit()

        resp = auth_client.post(
            "/api/discovery/rerun",
            headers={"Authorization": f"Bearer {raw}"},
        )
        assert resp.status_code == 403

    def test_rerun_allows_discovery_write(
        self, auth_client: TestClient
    ):
        with _make_session(auth_client) as session:
            raw = _create_api_key(session, scopes=["discovery:write"])
            session.commit()

        resp = auth_client.post(
            "/api/discovery/rerun",
            headers={"Authorization": f"Bearer {raw}"},
        )
        # 202 accepted or 500 (if Redis/Celery unavailable) are both OK
        assert resp.status_code not in (401, 403)

    def test_rerun_without_triggered_by_allows_write(
        self, auth_client: TestClient
    ):
        with _make_session(auth_client) as session:
            raw = _create_api_key(session, scopes=["discovery:write"])
            session.commit()

        resp = auth_client.post(
            "/api/discovery/rerun",
            headers={"Authorization": f"Bearer {raw}"},
            json={},
        )
        assert resp.status_code not in (401, 403)

    def test_config_write_does_not_grant_discovery_write(
        self, auth_client: TestClient
    ):
        with _make_session(auth_client) as session:
            raw = _create_api_key(session, scopes=["config:write"])
            session.commit()

        resp = auth_client.post(
            "/api/discovery/rerun",
            headers={"Authorization": f"Bearer {raw}"},
        )
        assert resp.status_code == 403
