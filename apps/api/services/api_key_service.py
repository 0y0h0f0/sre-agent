"""API key management service."""

from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta
from typing import Any

from sqlalchemy.orm import Session

from apps.api.schemas.api_keys import (
    ApiKeyCreateRequest,
    ApiKeyCreateResponse,
    ApiKeyListItem,
    ApiKeyListResponse,
)
from packages.common.errors import NotFoundError
from packages.common.time import utc_now
from packages.db.repositories.api_keys import ApiKeyRepository


class ApiKeyService:
    def __init__(self, db: Session) -> None:
        self._db = db
        self._repo = ApiKeyRepository(db)

    def create(
        self,
        data: ApiKeyCreateRequest,
        *,
        created_by: str = "admin",
    ) -> ApiKeyCreateResponse:
        raw_key = secrets.token_hex(32)  # 64 hex chars
        key_hash = _hash_key(raw_key)
        expires_at = None
        if data.expires_in_days:
            expires_at = utc_now() + timedelta(days=data.expires_in_days)

        key = self._repo.create(
            description=data.description,
            key_hash=key_hash,
            created_by=created_by,
            scopes=_dedupe(data.scopes),
            roles=_dedupe(data.roles),
            expires_at=expires_at,
        )
        self._db.commit()
        return ApiKeyCreateResponse(
            key_id=key.key_id,
            description=key.description,
            raw_key=raw_key,
            created_by=key.created_by,
            scopes=list(key.scopes) if key.scopes else [],
            roles=list(key.roles) if key.roles else [],
            expires_at=key.expires_at,
            created_at=key.created_at,
        )

    def list_all(self) -> ApiKeyListResponse:
        keys = self._repo.list_all()
        return ApiKeyListResponse(
            items=[ApiKeyListItem.model_validate(k) for k in keys],
            total=len(keys),
        )

    def has_any_keys(self) -> bool:
        """Return whether any API key exists.

        Bootstrap seed auth is intentionally limited to a pristine key store.
        Revoked/expired keys still count so the seed cannot become a recovery
        backdoor after first use.
        """
        return self._repo.has_any()

    def revoke(self, key_id: str) -> None:
        if not self._repo.revoke(key_id):
            raise NotFoundError("api_key", key_id)
        self._db.commit()

    def verify(self, raw_key: str) -> dict[str, Any] | None:
        """Verify a raw key and return identity info, or None if invalid."""
        key_hash = _hash_key(raw_key)
        key = self._repo.get_by_hash(key_hash)
        if key is None:
            return None
        if key.revoked:
            return None
        if key.expires_at is not None and key.expires_at < utc_now():
            return None
        return {
            "key_id": key.key_id,
            "description": key.description,
            "created_by": key.created_by,
            "scopes": list(key.scopes) if key.scopes else [],
            "roles": list(key.roles) if key.roles else [],
        }

    def touch_used(self, key_id: str) -> None:
        self._repo.touch_last_used(key_id)


def _hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def _dedupe(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = value.strip()
        if not normalized or normalized in seen:
            continue
        result.append(normalized)
        seen.add(normalized)
    return result
