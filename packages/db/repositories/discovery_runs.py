"""Repository for discovery_runs table."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from packages.common.ids import new_id
from packages.common.time import utc_now
from packages.db.models import DiscoveryRun


class DiscoveryRunRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def create(
        self,
        *,
        source: str,
        trigger_type: str = "automatic",
        triggered_by: str | None = None,
        summary: dict[str, Any] | None = None,
    ) -> DiscoveryRun:
        run = DiscoveryRun(
            discovery_run_id=new_id("dr_"),
            source=source,
            status="running",
            trigger_type=trigger_type,
            triggered_by=triggered_by,
            started_at=utc_now(),
            summary=summary or {},
        )
        self.db.add(run)
        return run

    def get_by_id(self, discovery_run_id: str) -> DiscoveryRun | None:
        stmt = select(DiscoveryRun).where(
            DiscoveryRun.discovery_run_id == discovery_run_id
        )
        return self.db.scalars(stmt).first()

    def list_recent(self, limit: int = 20) -> Sequence[DiscoveryRun]:
        stmt = (
            select(DiscoveryRun)
            .order_by(DiscoveryRun.created_at.desc())
            .limit(limit)
        )
        return self.db.scalars(stmt).all()

    def mark_finished(
        self,
        run: DiscoveryRun,
        *,
        status: str,
        error_message: str | None = None,
        summary: dict[str, Any] | None = None,
    ) -> None:
        run.status = status
        run.finished_at = utc_now()
        if error_message is not None:
            run.error_message = error_message
        if summary is not None:
            run.summary = summary
        self.db.flush()
