"""Config API schemas — request/response models for config endpoints.

M5 PR 5.3: Config publish, rollback, revoke, current, versions.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Publish
# ---------------------------------------------------------------------------


class ConfigPublishRequest(BaseModel):
    """Request to publish a new effective config version."""

    config_snapshot: dict[str, Any] = Field(default_factory=dict)
    proposal_id: str | None = None
    published_by: str | None = None
    stale_after_days: int = Field(default=30, ge=1, le=365)


class ConfigPublishResponse(BaseModel):
    """Response after publishing a config version."""

    version_id: str
    version_number: int
    status: str
    published_at: datetime | None = None
    stale_warning_at: datetime | None = None


# ---------------------------------------------------------------------------
# Rollback
# ---------------------------------------------------------------------------


class ConfigRollbackRequest(BaseModel):
    """Request to rollback a config version."""

    version_id: str
    rolled_back_by: str | None = None


class ConfigRollbackResponse(BaseModel):
    """Response after rolling back a config version."""

    version_id: str
    version_number: int
    status: str
    published_at: datetime | None = None
    message: str = ""


# ---------------------------------------------------------------------------
# Revoke
# ---------------------------------------------------------------------------


class ConfigRevokeRequest(BaseModel):
    """Request to revoke a config version."""

    version_id: str
    revoked_by: str | None = None
    reason: str | None = None


class ConfigRevokeResponse(BaseModel):
    """Response after revoking a config version."""

    version_id: str
    version_number: int
    status: str
    revoked_at: datetime | None = None
    message: str = ""


# ---------------------------------------------------------------------------
# Current / Versions (read)
# ---------------------------------------------------------------------------


class ConfigVersionSummary(BaseModel):
    """Summary of a single config version."""

    version_id: str
    version_number: int
    status: str
    published_at: datetime | None = None
    published_by: str | None = None
    stale_warning_at: datetime | None = None
    stale_after_days: int = 30
    proposal_id: str | None = None

    model_config = {"from_attributes": True}


class ConfigCurrentResponse(BaseModel):
    """The currently active published config."""

    version_id: str | None = None
    version_number: int | None = None
    status: str = "none"
    config_snapshot: dict[str, Any] | None = None
    published_at: datetime | None = None
    stale_warning_at: datetime | None = None
    is_stale: bool = False


class ConfigVersionsResponse(BaseModel):
    """List of config versions."""

    versions: list[ConfigVersionSummary] = Field(default_factory=list)
    total: int = 0


# ---------------------------------------------------------------------------
# Override (PR 5.4)
# ---------------------------------------------------------------------------


class OverrideCreateRequest(BaseModel):
    """Request to create a discovery override."""

    backend_type: str  # prometheus | loki | jaeger | alertmanager
    override_json: dict[str, Any] = Field(default_factory=dict)
    reason: str = Field(min_length=1, max_length=2000)
    expires_at: datetime | None = None  # auto-set to default TTL if omitted
    created_by_key_id: str | None = None


class OverrideResponse(BaseModel):
    """Response for an override."""

    override_id: str
    backend_type: str
    override_json: dict[str, Any] = Field(default_factory=dict)
    reason: str
    expires_at: datetime | None = None
    revoked_at: datetime | None = None
    revoke_reason: str | None = None
    created_by_key_id: str | None = None
    created_at: datetime | None = None
    is_active: bool = True

    model_config = {"from_attributes": True}


class OverrideListResponse(BaseModel):
    """List of active overrides."""

    overrides: list[OverrideResponse] = Field(default_factory=list)
    total: int = 0


class OverrideRevokeRequest(BaseModel):
    """Request to revoke an override."""

    reason: str | None = None
    revoked_by: str | None = None


class OverrideRevokeResponse(BaseModel):
    """Response after revoking an override."""

    override_id: str
    status: str = "revoked"
    message: str = ""
