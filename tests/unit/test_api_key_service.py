"""Unit tests for API key service."""

from __future__ import annotations

from datetime import timedelta

from apps.api.schemas.api_keys import ApiKeyCreateRequest
from apps.api.services.api_key_service import ApiKeyService, _hash_key
from packages.common.time import utc_now


def test_hash_key_deterministic() -> None:
    raw = "test-key-12345"
    h1 = _hash_key(raw)
    h2 = _hash_key(raw)
    assert h1 == h2
    assert len(h1) == 64  # SHA-256 hex


def test_hash_key_different_inputs() -> None:
    h1 = _hash_key("key-a")
    h2 = _hash_key("key-b")
    assert h1 != h2


def test_create_key_returns_raw_key_once(db_session) -> None:
    service = ApiKeyService(db_session)
    data = ApiKeyCreateRequest(description="test-key")
    response = service.create(data)
    assert response.key_id.startswith("apik_")
    assert response.description == "test-key"
    assert len(response.raw_key) == 64  # token_hex(32)
    assert response.created_by == "admin"
    assert response.scopes == []
    assert response.roles == []
    assert response.expires_at is None


def test_create_key_persists_scopes_roles_and_creator(db_session) -> None:
    service = ApiKeyService(db_session)
    data = ApiKeyCreateRequest(
        description="scoped-key",
        scopes=["config:read", "config:read", "api_key:admin"],
        roles=["operator", "operator"],
    )
    response = service.create(data, created_by="apik_admin")

    assert response.created_by == "apik_admin"
    assert response.scopes == ["config:read", "api_key:admin"]
    assert response.roles == ["operator"]

    identity = service.verify(response.raw_key)
    assert identity is not None
    assert identity["scopes"] == ["config:read", "api_key:admin"]
    assert identity["roles"] == ["operator"]


def test_create_request_rejects_unknown_scope() -> None:
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="unsupported API key scope"):
        ApiKeyCreateRequest(description="bad-scope", scopes=["made_up:scope"])


def test_create_request_rejects_invalid_role() -> None:
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="roles must start"):
        ApiKeyCreateRequest(description="bad-role", roles=["Admin Role"])


def test_has_any_keys_tracks_bootstrap_initialization(db_session) -> None:
    service = ApiKeyService(db_session)
    assert service.has_any_keys() is False

    service.create(ApiKeyCreateRequest(description="first-key"))

    assert service.has_any_keys() is True


def test_create_key_with_expiry(db_session) -> None:
    service = ApiKeyService(db_session)
    data = ApiKeyCreateRequest(description="expiring-key", expires_in_days=7)
    response = service.create(data)
    assert response.expires_at is not None
    expected = utc_now() + timedelta(days=7)
    delta = abs((response.expires_at - expected).total_seconds())
    assert delta < 5


def test_verify_valid_key(db_session) -> None:
    service = ApiKeyService(db_session)
    data = ApiKeyCreateRequest(description="verify-test")
    created = service.create(data)

    identity = service.verify(created.raw_key)
    assert identity is not None
    assert identity["key_id"] == created.key_id


def test_verify_invalid_key(db_session) -> None:
    service = ApiKeyService(db_session)
    identity = service.verify("nonexistent-key")
    assert identity is None


def test_verify_revoked_key(db_session) -> None:
    service = ApiKeyService(db_session)
    data = ApiKeyCreateRequest(description="revoke-me")
    created = service.create(data)

    service.revoke(created.key_id)
    identity = service.verify(created.raw_key)
    assert identity is None


def test_list_keys(db_session) -> None:
    service = ApiKeyService(db_session)
    service.create(ApiKeyCreateRequest(description="key-1"))
    service.create(ApiKeyCreateRequest(description="key-2"))
    result = service.list_all()
    assert result.total == 2
    assert len(result.items) == 2


def test_revoke_nonexistent_raises(db_session) -> None:
    import pytest

    from packages.common.errors import NotFoundError

    service = ApiKeyService(db_session)
    with pytest.raises(NotFoundError):
        service.revoke("apik_nonexistent")
