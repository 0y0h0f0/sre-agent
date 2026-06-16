"""Config API router — publish, rollback, revoke, and read endpoints.

M5 PR 5.3: POST /api/config/publish, /rollback, /revoke; GET /current, /versions.
Write endpoints require ``config:write`` scope.
Read endpoints require ``config:read`` or ``config:write`` scope.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from apps.api.dependencies import get_app_settings, get_db, require_scope
from apps.api.schemas.config import (
    ConfigCurrentResponse,
    ConfigPublishRequest,
    ConfigPublishResponse,
    ConfigRevokeRequest,
    ConfigRevokeResponse,
    ConfigRollbackRequest,
    ConfigRollbackResponse,
    ConfigVersionsResponse,
    ConfigVersionSummary,
    OverrideCreateRequest,
    OverrideListResponse,
    OverrideResponse,
    OverrideRevokeRequest,
    OverrideRevokeResponse,
)
from packages.common.settings import Settings
from packages.discovery.config_publisher import (
    ConfigPublisher,
    ConfigPublishError,
    ConfigRevokeError,
    ConfigRollbackError,
)

router = APIRouter(prefix="/api/config", tags=["config"])

_require_read = require_scope("config:read", "config:write")
_require_write = require_scope("config:write")


def _parse_comma_list(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()] if value else []


# ---------------------------------------------------------------------------
# GET /api/config/current
# ---------------------------------------------------------------------------


@router.get(
    "/current",
    response_model=ConfigCurrentResponse,
    dependencies=[Depends(_require_read)],
)
def get_config_current(
    db: Session = Depends(get_db),
) -> ConfigCurrentResponse:
    """Return the currently active published config."""
    publisher = ConfigPublisher(db)
    version = publisher._repo.get_latest_published()

    if version is None:
        return ConfigCurrentResponse(status="none")

    return ConfigCurrentResponse(
        version_id=version.version_id,
        version_number=version.version_number,
        status=version.status,
        config_snapshot=version.config_snapshot,
        published_at=version.published_at,
        stale_warning_at=version.stale_warning_at,
        is_stale=publisher.is_stale(version),
    )


# ---------------------------------------------------------------------------
# GET /api/config/versions
# ---------------------------------------------------------------------------


@router.get(
    "/versions",
    response_model=ConfigVersionsResponse,
    dependencies=[Depends(_require_read)],
)
def get_config_versions(
    db: Session = Depends(get_db),
    limit: int = Query(default=10, ge=1, le=100),
) -> ConfigVersionsResponse:
    """List recent config versions."""
    publisher = ConfigPublisher(db)
    versions = publisher.list_all_versions(limit=limit)
    return ConfigVersionsResponse(
        versions=[
            ConfigVersionSummary(
                version_id=v.version_id,
                version_number=v.version_number,
                status=v.status,
                published_at=v.published_at,
                published_by=v.published_by,
                stale_warning_at=v.stale_warning_at,
                stale_after_days=v.stale_after_days,
                proposal_id=v.proposal_id,
            )
            for v in versions
        ],
        total=len(versions),
    )


# ---------------------------------------------------------------------------
# POST /api/config/publish
# ---------------------------------------------------------------------------


@router.post(
    "/publish",
    response_model=ConfigPublishResponse,
    status_code=201,
    dependencies=[Depends(_require_write)],
)
def config_publish(
    body: ConfigPublishRequest,
    db: Session = Depends(get_db),
) -> ConfigPublishResponse:
    """Publish a new effective config version.

    The previous published version is superseded. The new version becomes
    the active published config that workers read.

    Requires ``config:write`` scope.
    """
    publisher = ConfigPublisher(db)
    try:
        version = publisher.publish(
            config_snapshot=body.config_snapshot,
            proposal_id=body.proposal_id,
            published_by=body.published_by,
            stale_after_days=body.stale_after_days,
        )
        db.commit()
        return ConfigPublishResponse(
            version_id=version.version_id,
            version_number=version.version_number,
            status=version.status,
            published_at=version.published_at,
            stale_warning_at=version.stale_warning_at,
        )
    except ConfigPublishError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# POST /api/config/rollback
# ---------------------------------------------------------------------------


@router.post(
    "/rollback",
    response_model=ConfigRollbackResponse,
    dependencies=[Depends(_require_write)],
)
def config_rollback(
    body: ConfigRollbackRequest,
    db: Session = Depends(get_db),
) -> ConfigRollbackResponse:
    """Rollback to the previous published config version.

    The specified version is rolled back, and the most recent superseded
    version (if any) is re-published.

    Requires ``config:write`` scope.
    """
    publisher = ConfigPublisher(db)
    try:
        version = publisher.rollback(
            body.version_id,
            rolled_back_by=body.rolled_back_by,
        )
        db.commit()
        return ConfigRollbackResponse(
            version_id=version.version_id,
            version_number=version.version_number,
            status=version.status,
            published_at=version.published_at,
            message=f"Rolled back to version {version.version_number}",
        )
    except ConfigRollbackError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# POST /api/config/revoke
# ---------------------------------------------------------------------------


@router.post(
    "/revoke",
    response_model=ConfigRevokeResponse,
    dependencies=[Depends(_require_write)],
)
def config_revoke(
    body: ConfigRevokeRequest,
    db: Session = Depends(get_db),
) -> ConfigRevokeResponse:
    """Revoke a config version.

    The version is marked as revoked and removed from worker selection.
    If no other published version exists, workers will rely on
    env/profile/defaults.

    Requires ``config:write`` scope.
    """
    publisher = ConfigPublisher(db)
    try:
        version = publisher.revoke(
            body.version_id,
            revoked_by=body.revoked_by,
            reason=body.reason,
        )
        db.commit()
        return ConfigRevokeResponse(
            version_id=version.version_id,
            version_number=version.version_number,
            status=version.status,
            revoked_at=version.revoked_at,
            message=f"Version {version.version_number} revoked",
        )
    except ConfigRevokeError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Override endpoints (PR 5.4)
# ---------------------------------------------------------------------------

# Default TTLs by override type: backend URL gets 7 days, label/mapping gets 14 days.
_OVERRIDE_DEFAULT_TTL_DAYS: dict[str, int] = {
    "prometheus": 7,
    "loki": 7,
    "jaeger": 7,
    "alertmanager": 7,
}
_OVERRIDE_MAX_TTL_DAYS = 30

# Fields forbidden in override_json — secret/auth/executor/live must not
# be set via the general override API.
_FORBIDDEN_OVERRIDE_FIELDS = {
    "secret", "secrets", "auth", "auth_config",
    "executor_backend", "executor", "live", "bearer_token",
    "password", "private_key", "client_cert", "client_key",
}


@router.get(
    "/overrides",
    response_model=OverrideListResponse,
    dependencies=[Depends(_require_read)],
)
def get_overrides(
    db: Session = Depends(get_db),
) -> OverrideListResponse:
    """List all active overrides (not revoked, not expired)."""
    from packages.db.repositories.discovery_overrides import (
        DiscoveryOverrideRepository,
    )

    repo = DiscoveryOverrideRepository(db)
    active = repo.list_active()

    return OverrideListResponse(
        overrides=[
            OverrideResponse(
                override_id=ov.override_id,
                backend_type=ov.backend_type,
                override_json=ov.override_json,
                reason=ov.reason,
                expires_at=ov.expires_at,
                revoked_at=ov.revoked_at,
                revoke_reason=ov.revoke_reason,
                created_by_key_id=ov.created_by_key_id,
                created_at=ov.created_at,
                is_active=True,
            )
            for ov in active
        ],
        total=len(active),
    )


@router.post(
    "/overrides",
    response_model=OverrideResponse,
    status_code=201,
    dependencies=[Depends(_require_write)],
)
def create_override(
    body: OverrideCreateRequest,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_app_settings),
) -> OverrideResponse:
    """Create a new config override.

    - ``reason`` is required.
    - ``expires_at`` is auto-set to the default TTL (7 days) if omitted.
    - Max TTL is 30 days.
    - Override must not contain secret/auth/executor/live fields.
    - Backend URL overrides are validated for safety.

    Requires ``config:write`` scope.
    """
    from datetime import timedelta

    from packages.common.backend_url_safety import BackendUrlSafetyValidator
    from packages.common.time import utc_now
    from packages.db.repositories.discovery_overrides import (
        DiscoveryOverrideRepository,
    )

    # Validate no forbidden fields.
    for forbidden in _FORBIDDEN_OVERRIDE_FIELDS:
        if forbidden in body.override_json:
            raise HTTPException(
                status_code=400,
                detail=f"Cannot override '{forbidden}' via general override API",
            )

    # Validate backend URL if present.
    url = body.override_json.get("url")
    if url and isinstance(url, str):
        validator = BackendUrlSafetyValidator(
            allowlist_patterns=_parse_comma_list(settings.backend_url_allowlist),
            app_env=settings.app_env,
        )
        result = validator.validate(url)
        if not result.is_safe:
            raise HTTPException(
                status_code=400,
                detail=f"Unsafe backend URL: {result.reason}",
            )

    # Determine expires_at.
    now = utc_now()
    default_ttl = _OVERRIDE_DEFAULT_TTL_DAYS.get(body.backend_type, 14)
    if body.expires_at is None:
        expires_at = now + timedelta(days=default_ttl)
    else:
        expires_at = body.expires_at
        max_expiry = now + timedelta(days=_OVERRIDE_MAX_TTL_DAYS)
        if expires_at > max_expiry:
            raise HTTPException(
                status_code=400,
                detail=f"Override TTL exceeds maximum of {_OVERRIDE_MAX_TTL_DAYS} days",
            )

    repo = DiscoveryOverrideRepository(db)
    override = repo.create(
        backend_type=body.backend_type,
        override_json=body.override_json,
        reason=body.reason,
        expires_at=expires_at,
        created_by_key_id=body.created_by_key_id,
    )
    db.commit()

    return OverrideResponse(
        override_id=override.override_id,
        backend_type=override.backend_type,
        override_json=override.override_json,
        reason=override.reason,
        expires_at=override.expires_at,
        revoked_at=override.revoked_at,
        revoke_reason=override.revoke_reason,
        created_by_key_id=override.created_by_key_id,
        created_at=override.created_at,
        is_active=True,
    )


@router.delete(
    "/overrides/{override_id}",
    response_model=OverrideRevokeResponse,
    dependencies=[Depends(_require_write)],
)
def revoke_override(
    override_id: str,
    body: OverrideRevokeRequest | None = None,
    db: Session = Depends(get_db),
) -> OverrideRevokeResponse:
    """Revoke an active override.

    Revoked overrides are retained for audit but do not participate
    in EffectiveConfig merge.

    Requires ``config:write`` scope.
    """
    from packages.common.time import utc_now
    from packages.db.repositories.discovery_overrides import (
        DiscoveryOverrideRepository,
    )

    repo = DiscoveryOverrideRepository(db)
    override = repo.get_by_id(override_id)

    if override is None:
        raise HTTPException(
            status_code=404,
            detail=f"Override '{override_id}' not found",
        )
    if override.revoked_at is not None:
        raise HTTPException(
            status_code=400,
            detail="Override is already revoked",
        )

    override.revoked_at = utc_now()
    if body and body.reason:
        override.revoke_reason = body.reason
    db.commit()

    return OverrideRevokeResponse(
        override_id=override_id,
        status="revoked",
        message=f"Override '{override_id}' revoked",
    )
