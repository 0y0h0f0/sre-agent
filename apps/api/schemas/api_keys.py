"""Pydantic schemas for API key management."""

from __future__ import annotations

import re
from datetime import datetime

from pydantic import BaseModel, Field, field_validator

_ALLOWED_SCOPES = frozenset(
    {
        "api_key:admin",
        "config:read",
        "config:write",
        "discovery:read",
        "discovery:write",
        "runbook:read",
        "runbook:review",
        "runbook:web_search",
        "runbook:llm_generate",
        "incident:llm_diff",
        "llm:invoke",
        "ai:external",
        "embedding:external",
    }
)
_ROLE_RE = re.compile(r"^[a-z][a-z0-9_:-]{0,63}$")


class ApiKeyCreateRequest(BaseModel):
    description: str = Field(..., min_length=1, max_length=255)
    expires_in_days: int | None = Field(default=None, gt=0, le=365)
    scopes: list[str] = Field(default_factory=list, max_length=50)
    roles: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("scopes")
    @classmethod
    def _validate_scopes(cls, values: list[str]) -> list[str]:
        invalid = sorted(
            {
                value
                for value in values
                if not isinstance(value, str) or value.strip() not in _ALLOWED_SCOPES
            }
        )
        if invalid:
            raise ValueError(f"unsupported API key scope(s): {', '.join(invalid)}")
        return [value.strip() for value in values]

    @field_validator("roles")
    @classmethod
    def _validate_roles(cls, values: list[str]) -> list[str]:
        invalid = sorted(
            {
                value
                for value in values
                if not isinstance(value, str) or not _ROLE_RE.match(value.strip())
            }
        )
        if invalid:
            raise ValueError(
                "roles must start with a lowercase letter and contain only "
                "lowercase letters, digits, '_', ':', or '-'"
            )
        return [value.strip() for value in values]


class ApiKeyCreateResponse(BaseModel):
    key_id: str
    description: str
    raw_key: str
    created_by: str
    scopes: list[str]
    roles: list[str]
    expires_at: datetime | None
    created_at: datetime


class ApiKeyListItem(BaseModel):
    key_id: str
    description: str
    created_by: str
    expires_at: datetime | None
    last_used_at: datetime | None
    revoked: bool
    scopes: list[str]
    roles: list[str]
    created_at: datetime

    model_config = {"from_attributes": True}


class ApiKeyListResponse(BaseModel):
    items: list[ApiKeyListItem]
    total: int
