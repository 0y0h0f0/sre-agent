"""Repository for approval_groups table — team-based approval routing."""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from packages.common.ids import new_id
from packages.db.models import ApprovalGroup

logger = logging.getLogger(__name__)


class ApprovalGroupRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def create(
        self,
        *,
        name: str,
        service_pattern: str,
        members: list[str] | None = None,
        is_default: bool = False,
    ) -> ApprovalGroup:
        group = ApprovalGroup(
            group_id=new_id("agp_"),
            name=name,
            service_pattern=service_pattern,
            members=members or [],
            is_default=is_default,
        )
        self.db.add(group)
        return group

    def get_by_id(self, group_id: str) -> ApprovalGroup | None:
        stmt = select(ApprovalGroup).where(ApprovalGroup.group_id == group_id)
        return self.db.scalar(stmt)

    def get_by_name(self, name: str) -> ApprovalGroup | None:
        stmt = select(ApprovalGroup).where(ApprovalGroup.name == name)
        return self.db.scalar(stmt)

    def list_all(self) -> Sequence[ApprovalGroup]:
        stmt = select(ApprovalGroup).order_by(ApprovalGroup.name.asc())
        return self.db.scalars(stmt).all()

    def find_by_service(self, service: str, limit: int = 100) -> ApprovalGroup | None:
        """Return the first group whose service_pattern regex matches the given service.

        Loads at most ``limit`` groups. For deployments with many groups,
        consider moving to SQL-level regex (Postgres ``~`` operator).
        """
        all_groups = self.list_all()[:limit]
        for group in all_groups:
            try:
                if re.fullmatch(group.service_pattern, service):
                    return group
            except re.error:
                logger.warning(
                    "invalid regex in approval group %s (%s): %s",
                    group.group_id, group.name, group.service_pattern, exc_info=True,
                )
        return None

    def update(
        self,
        group_id: str,
        *,
        name: str | None = None,
        service_pattern: str | None = None,
        members: list[str] | None = None,
        is_default: bool | None = None,
    ) -> ApprovalGroup | None:
        group = self.get_by_id(group_id)
        if group is None:
            return None
        if name is not None:
            group.name = name
        if service_pattern is not None:
            group.service_pattern = service_pattern
        if members is not None:
            group.members = members
        if is_default is not None:
            group.is_default = is_default
        return group

    def delete(self, group_id: str) -> bool:
        group = self.get_by_id(group_id)
        if group is None:
            return False
        self.db.delete(group)
        return True
