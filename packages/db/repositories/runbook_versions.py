"""Repository for RunbookVersion CRUD operations."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from packages.common.ids import new_id
from packages.db.models import RunbookVersion


class RunbookVersionRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def create(
        self,
        *,
        document_id: str,
        source_path: str,
        content_hash: str,
        change_reason: str,
        related_incident_id: str | None = None,
        related_draft_id: str | None = None,
        diff_from_previous: str | None = None,
        created_by: str = "agent",
    ) -> RunbookVersion:
        latest = self.get_latest(document_id, for_update=True)
        version_number = (latest.version_number + 1) if latest else 1

        version = RunbookVersion(
            version_id=new_id("ver_"),
            document_id=document_id,
            version_number=version_number,
            source_path=source_path,
            content_hash=content_hash,
            change_reason=change_reason,
            related_incident_id=related_incident_id,
            related_draft_id=related_draft_id,
            diff_from_previous=diff_from_previous,
            created_by=created_by,
        )
        self.db.add(version)
        self.db.flush()
        return version

    def get_latest(self, document_id: str, *, for_update: bool = False) -> RunbookVersion | None:
        stmt = (
            select(RunbookVersion)
            .where(RunbookVersion.document_id == document_id)
            .order_by(RunbookVersion.version_number.desc())
            .limit(1)
        )
        if for_update:
            stmt = stmt.with_for_update()
        return self.db.scalar(stmt)

    def list_versions(self, document_id: str) -> list[RunbookVersion]:
        stmt = (
            select(RunbookVersion)
            .where(RunbookVersion.document_id == document_id)
            .order_by(RunbookVersion.version_number.desc())
        )
        return list(self.db.scalars(stmt).all())

    def get_by_version_id(self, version_id: str) -> RunbookVersion | None:
        stmt = select(RunbookVersion).where(RunbookVersion.version_id == version_id)
        return self.db.scalar(stmt)
