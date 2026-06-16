"""Integration tests for API key admin scope enforcement."""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Generator
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from packages.common.settings import get_settings
from packages.db.base import Base
from packages.db.models import ApiKey


def _hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


@pytest.fixture()
def api_key_admin_client(
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[TestClient, None, None]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    test_session = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    monkeypatch.setenv("API_KEY_AUTH_ENABLED", "true")
    monkeypatch.setenv("API_KEY_INITIAL_SEED", "bootstrap-secret")
    monkeypatch.setenv("LLM_PROVIDER", "fake")
    monkeypatch.setenv("EMBEDDING_PROVIDER", "fake")
    get_settings.cache_clear()

    from apps.api.main import create_app

    app = create_app()
    with (
        patch("apps.api.middleware.auth.SessionLocal", test_session),
        patch("packages.db.session.SessionLocal", test_session),
    ):
        with TestClient(app) as client:
            client._test_session_factory = test_session  # type: ignore[attr-defined]
            yield client
    get_settings.cache_clear()
    Base.metadata.drop_all(engine)
    engine.dispose()


def _make_session(client: TestClient):
    factory = getattr(client, "_test_session_factory", None)
    if factory is None:
        raise RuntimeError("Test session factory not available")
    return factory()


def _create_key(client: TestClient, *, scopes: list[str]) -> str:
    raw_key = secrets.token_hex(32)
    with _make_session(client) as session:
        session.add(
            ApiKey(
                key_id=f"apik_test_{secrets.token_hex(4)}",
                description="test-key",
                key_hash=_hash_key(raw_key),
                scopes=scopes,
            )
        )
        session.commit()
    return raw_key


def test_api_key_admin_scope_required(api_key_admin_client: TestClient) -> None:
    raw_key = _create_key(api_key_admin_client, scopes=["config:read"])

    response = api_key_admin_client.get(
        "/api/api-keys",
        headers={"Authorization": f"Bearer {raw_key}"},
    )

    assert response.status_code == 403
    body = response.json()
    assert body["error"]["code"] == "FORBIDDEN"
    assert "api_key:admin" in body["error"]["message"]


def test_bootstrap_seed_can_create_scoped_admin_key(
    api_key_admin_client: TestClient,
) -> None:
    response = api_key_admin_client.post(
        "/api/api-keys",
        headers={"Authorization": "Bearer bootstrap-secret"},
        json={
            "description": "operator-admin",
            "scopes": ["api_key:admin", "config:write"],
            "roles": ["operator"],
        },
    )

    assert response.status_code == 201
    body = response.json()
    assert body["created_by"] == "apik_initial"
    assert body["scopes"] == ["api_key:admin", "config:write"]
    assert body["roles"] == ["operator"]

    listed = api_key_admin_client.get(
        "/api/api-keys",
        headers={"Authorization": f"Bearer {body['raw_key']}"},
    )
    assert listed.status_code == 200
    assert listed.json()["items"][0]["scopes"] == ["api_key:admin", "config:write"]


def test_create_api_key_rejects_unknown_scope(
    api_key_admin_client: TestClient,
) -> None:
    response = api_key_admin_client.post(
        "/api/api-keys",
        headers={"Authorization": "Bearer bootstrap-secret"},
        json={
            "description": "bad-scope",
            "scopes": ["made_up:scope"],
            "roles": ["operator"],
        },
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_bootstrap_seed_rejected_after_first_key_exists(
    api_key_admin_client: TestClient,
) -> None:
    first = api_key_admin_client.post(
        "/api/api-keys",
        headers={"Authorization": "Bearer bootstrap-secret"},
        json={
            "description": "first-admin",
            "scopes": ["api_key:admin"],
            "roles": ["operator"],
        },
    )
    assert first.status_code == 201

    second = api_key_admin_client.post(
        "/api/api-keys",
        headers={"Authorization": "Bearer bootstrap-secret"},
        json={
            "description": "second-admin",
            "scopes": ["api_key:admin"],
            "roles": ["operator"],
        },
    )

    assert second.status_code == 401
    assert second.json()["error"]["code"] == "UNAUTHORIZED"
