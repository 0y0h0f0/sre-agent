"""Repository for discovery_overrides table."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from packages.common.ids import new_id
from packages.common.time import utc_now
from packages.db.models import DiscoveryOverride


class DiscoveryOverrideRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def create(
        self,
        *,
        backend_type: str,
        override_json: dict[str, Any] | None = None,
        reason: str,
        expires_at: datetime,
        created_by_key_id: str | None = None,
        created_by_scopes: list[str] | None = None,
    ) -> DiscoveryOverride:
        override = DiscoveryOverride(
            override_id=new_id("dov_"),
            backend_type=backend_type,
            override_json=override_json or {},
            reason=reason,
            expires_at=expires_at,
            created_by_key_id=created_by_key_id,
            created_by_scopes=created_by_scopes or [],
        )
        self.db.add(override)
        return override

    def get_by_id(self, override_id: str) -> DiscoveryOverride | None:
        stmt = select(DiscoveryOverride).where(
            DiscoveryOverride.override_id == override_id
        )
        return self.db.scalars(stmt).first()

    def list_active(
        self, now: datetime | None = None
    ) -> Sequence[DiscoveryOverride]:
        """Return active overrides: not revoked AND not expired."""
        if now is None:
            now = utc_now()
        stmt = (
            select(DiscoveryOverride)
            .where(
                DiscoveryOverride.revoked_at.is_(None),
                DiscoveryOverride.expires_at > now,
            )
            .order_by(DiscoveryOverride.created_at.desc())
        )
        return self.db.scalars(stmt).all()

    def list_active_for_backend(
        self, backend_type: str, now: datetime | None = None
    ) -> Sequence[DiscoveryOverride]:
        """Return active overrides for a specific backend type."""
        if now is None:
            now = utc_now()
        stmt = (
            select(DiscoveryOverride)
            .where(
                DiscoveryOverride.backend_type == backend_type,
                DiscoveryOverride.revoked_at.is_(None),
                DiscoveryOverride.expires_at > now,
            )
            .order_by(DiscoveryOverride.created_at.desc())
        )
        return self.db.scalars(stmt).all()
