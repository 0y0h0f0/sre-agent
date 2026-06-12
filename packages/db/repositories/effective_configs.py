"""Repository for effective_config_versions table."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from packages.common.ids import new_id
from packages.common.time import utc_now
from packages.db.models import EffectiveConfigVersion


class EffectiveConfigRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def create(
        self,
        *,
        proposal_id: str | None = None,
        version_number: int,
        config_snapshot: dict[str, Any] | None = None,
        published_by: str | None = None,
        stale_after_days: int = 30,
    ) -> EffectiveConfigVersion:
        version = EffectiveConfigVersion(
            version_id=new_id("ecv_"),
            proposal_id=proposal_id,
            version_number=version_number,
            status="published",
            config_snapshot=config_snapshot or {},
            published_at=utc_now(),
            published_by=published_by,
            stale_after_days=stale_after_days,
        )
        self.db.add(version)
        return version

    def get_by_id(self, version_id: str) -> EffectiveConfigVersion | None:
        stmt = select(EffectiveConfigVersion).where(
            EffectiveConfigVersion.version_id == version_id
        )
        return self.db.scalars(stmt).first()

    def get_latest_published(self) -> EffectiveConfigVersion | None:
        stmt = (
            select(EffectiveConfigVersion)
            .where(EffectiveConfigVersion.status == "published")
            .order_by(EffectiveConfigVersion.version_number.desc())
            .limit(1)
        )
        return self.db.scalars(stmt).first()

    def list_published(self, limit: int = 10) -> Sequence[EffectiveConfigVersion]:
        stmt = (
            select(EffectiveConfigVersion)
            .where(EffectiveConfigVersion.status == "published")
            .order_by(EffectiveConfigVersion.version_number.desc())
            .limit(limit)
        )
        return self.db.scalars(stmt).all()
