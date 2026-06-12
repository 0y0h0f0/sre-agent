"""Auth tests for override API — PR 5.4."""

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
    raw_key = secrets.token_hex(32)
    key = ApiKey(
        key_id=f"apik_test_{secrets.token_hex(4)}",
        description="auth-test-key",
        key_hash=_hash_key(raw_key),
        scopes=scopes or [],
    )
    session.add(key)
    session.flush()
    return raw_key


@pytest.fixture()
def auth_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
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
            client._test_session_factory = TestSession  # type: ignore[attr-defined]
            yield client
    app.dependency_overrides.clear()
    get_settings.cache_clear()


def _make_session(auth_client: TestClient):
    factory = getattr(auth_client, "_test_session_factory", None)
    if factory is None:
        raise RuntimeError("Test session factory not available")
    return factory()


class TestOverrideReadAuth:
    def test_get_overrides_returns_401_without_auth(self, auth_client: TestClient):
        resp = auth_client.get("/api/config/overrides")
        assert resp.status_code == 401

    def test_get_overrides_allows_config_read(self, auth_client: TestClient):
        with _make_session(auth_client) as session:
            raw = _create_api_key(session, scopes=["config:read"])
            session.commit()
        resp = auth_client.get(
            "/api/config/overrides",
            headers={"Authorization": f"Bearer {raw}"},
        )
        assert resp.status_code == 200

    def test_get_overrides_allows_config_write(self, auth_client: TestClient):
        with _make_session(auth_client) as session:
            raw = _create_api_key(session, scopes=["config:write"])
            session.commit()
        resp = auth_client.get(
            "/api/config/overrides",
            headers={"Authorization": f"Bearer {raw}"},
        )
        assert resp.status_code == 200

    def test_get_overrides_rejects_no_matching_scope(self, auth_client: TestClient):
        with _make_session(auth_client) as session:
            raw = _create_api_key(session, scopes=["discovery:read"])
            session.commit()
        resp = auth_client.get(
            "/api/config/overrides",
            headers={"Authorization": f"Bearer {raw}"},
        )
        assert resp.status_code == 403


class TestOverrideWriteAuth:
    def test_create_override_returns_401_without_auth(self, auth_client: TestClient):
        resp = auth_client.post("/api/config/overrides", json={})
        assert resp.status_code == 401

    def test_create_override_rejects_config_read_only(
        self, auth_client: TestClient
    ):
        with _make_session(auth_client) as session:
            raw = _create_api_key(session, scopes=["config:read"])
            session.commit()
        resp = auth_client.post(
            "/api/config/overrides",
            headers={"Authorization": f"Bearer {raw}"},
            json={
                "backend_type": "prometheus",
                "override_json": {"url": "https://prom.example.com"},
                "reason": "test override",
            },
        )
        assert resp.status_code == 403

    def test_create_override_allows_config_write(self, auth_client: TestClient):
        with _make_session(auth_client) as session:
            raw = _create_api_key(session, scopes=["config:write"])
            session.commit()
        resp = auth_client.post(
            "/api/config/overrides",
            headers={"Authorization": f"Bearer {raw}"},
            json={
                "backend_type": "prometheus",
                "override_json": {"url": "https://prom.example.com"},
                "reason": "test override",
            },
        )
        assert resp.status_code not in (401, 403)

    def test_revoke_override_requires_config_write(self, auth_client: TestClient):
        with _make_session(auth_client) as session:
            raw = _create_api_key(session, scopes=["config:read"])
            session.commit()
        resp = auth_client.delete(
            "/api/config/overrides/test-ov",
            headers={"Authorization": f"Bearer {raw}"},
        )
        assert resp.status_code == 403

    def test_revoke_override_allows_config_write(self, auth_client: TestClient):
        with _make_session(auth_client) as session:
            raw = _create_api_key(session, scopes=["config:write"])
            session.commit()
        resp = auth_client.delete(
            "/api/config/overrides/test-ov",
            headers={"Authorization": f"Bearer {raw}"},
        )
        assert resp.status_code not in (401, 403)

    def test_discovery_write_does_not_grant_override_write(
        self, auth_client: TestClient
    ):
        with _make_session(auth_client) as session:
            raw = _create_api_key(session, scopes=["discovery:write"])
            session.commit()
        resp = auth_client.post(
            "/api/config/overrides",
            headers={"Authorization": f"Bearer {raw}"},
            json={
                "backend_type": "prometheus",
                "override_json": {"url": "https://prom.example.com"},
                "reason": "test",
            },
        )
        assert resp.status_code == 403
