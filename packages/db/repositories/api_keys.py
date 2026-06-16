"""Repository for API key CRUD."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select

from packages.common.ids import new_id
from packages.common.time import utc_now
from packages.db.models import ApiKey
from packages.db.session import SessionLike


class ApiKeyRepository:
    def __init__(self, db: SessionLike) -> None:
        self._db = db

    def create(
        self,
        *,
        description: str,
        key_hash: str,
        created_by: str = "admin",
        scopes: list[str] | None = None,
        roles: list[str] | None = None,
        expires_at: datetime | None = None,
    ) -> ApiKey:
        key = ApiKey(
            key_id=new_id("apik_"),
            description=description,
            key_hash=key_hash,
            created_by=created_by,
            scopes=scopes or [],
            roles=roles or [],
            expires_at=expires_at,
        )
        self._db.add(key)
        self._db.flush()
        return key

    def get_by_public_id(self, key_id: str) -> ApiKey | None:
        return self._db.scalars(
            select(ApiKey).where(ApiKey.key_id == key_id)
        ).one_or_none()

    def get_by_hash(self, key_hash: str) -> ApiKey | None:
        return self._db.scalars(
            select(ApiKey).where(ApiKey.key_hash == key_hash)
        ).one_or_none()

    def list_all(self) -> list[ApiKey]:
        return list(
            self._db.scalars(
                select(ApiKey).order_by(ApiKey.created_at.desc())
            ).all()
        )

    def has_any(self) -> bool:
        return self._db.scalar(select(ApiKey.id).limit(1)) is not None

    def revoke(self, key_id: str) -> bool:
        key = self.get_by_public_id(key_id)
        if key is None:
            return False
        key.revoked = True
        self._db.flush()
        return True

    def touch_last_used(self, key_id: str) -> None:
        key = self.get_by_public_id(key_id)
        if key is not None:
            key.last_used_at = utc_now()
            self._db.flush()
