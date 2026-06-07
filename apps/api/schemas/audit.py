"""Pydantic schema for audit log entries."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel


class AuditLogItem(BaseModel):
    audit_id: str
    incident_id: str | None = None
    actor: str
    action: str
    resource_type: str
    resource_id: str
    details: dict[str, Any]
    created_at: datetime


class AuditLogListResponse(BaseModel):
    items: list[AuditLogItem]
    total: int
