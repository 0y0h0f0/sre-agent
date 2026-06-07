"""Business logic for approval group management."""

from __future__ import annotations

from sqlalchemy.orm import Session

from apps.api.schemas.approval_groups import (
    ApprovalGroupCreate,
    ApprovalGroupItem,
    ApprovalGroupListResponse,
    ApprovalGroupUpdate,
)
from packages.common.errors import ConflictError, NotFoundError
from packages.db.repositories.approval_groups import ApprovalGroupRepository


class ApprovalGroupService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.groups = ApprovalGroupRepository(db)

    def create(self, data: ApprovalGroupCreate) -> ApprovalGroupItem:
        existing = self.groups.get_by_name(data.name)
        if existing is not None:
            raise ConflictError(
                "approval group name already exists",
                details={"name": data.name},
            )
        group = self.groups.create(
            name=data.name,
            service_pattern=data.service_pattern,
            members=data.members,
            is_default=data.is_default,
        )
        self.db.commit()
        return self._item(group)

    def get(self, group_id: str) -> ApprovalGroupItem:
        group = self.groups.get_by_id(group_id)
        if group is None:
            raise NotFoundError("approval_group", group_id)
        return self._item(group)

    def list_all(self) -> ApprovalGroupListResponse:
        items = self.groups.list_all()
        return ApprovalGroupListResponse(
            items=[self._item(g) for g in items],
            total=len(items),
        )

    def update(self, group_id: str, data: ApprovalGroupUpdate) -> ApprovalGroupItem:
        group = self.groups.update(
            group_id,
            name=data.name,
            service_pattern=data.service_pattern,
            members=data.members,
            is_default=data.is_default,
        )
        if group is None:
            raise NotFoundError("approval_group", group_id)
        self.db.commit()
        return self._item(group)

    def delete(self, group_id: str) -> None:
        if not self.groups.delete(group_id):
            raise NotFoundError("approval_group", group_id)
        self.db.commit()

    def _item(self, group) -> ApprovalGroupItem:
        return ApprovalGroupItem(
            group_id=group.group_id,
            name=group.name,
            service_pattern=group.service_pattern,
            members=group.members,
            is_default=group.is_default,
            created_at=group.created_at,
            updated_at=group.updated_at,
        )
