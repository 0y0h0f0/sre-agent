from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from apps.api.dependencies import (
    NotificationTaskEnqueue,
    TaskEnqueue,
    get_app_settings,
    get_current_api_key,
    get_db,
    get_notification_task_enqueue,
    get_task_enqueue,
)
from apps.api.rate_limit import RateLimiter, build_rate_limiter
from apps.api.schemas.alerts import AlertCreateRequest, AlertCreateResponse
from apps.api.services.alert_service import AlertService
from packages.common.errors import TooManyRequestsError
from packages.common.settings import Settings

router = APIRouter(prefix="/api/alerts", tags=["alerts"])


def _rate_limit_key(
    request: Request, api_key: dict[str, str] = Depends(get_current_api_key)
) -> str:
    """Resolve a rate-limit identifier: api_key_id or client IP."""
    return api_key.get("key_id") or (request.client.host if request.client else "unknown")


@router.post("", response_model=AlertCreateResponse, status_code=202)
def create_alert(
    payload: AlertCreateRequest,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_app_settings),
    enqueue_diagnosis: TaskEnqueue = Depends(get_task_enqueue),
    enqueue_notification: NotificationTaskEnqueue = Depends(get_notification_task_enqueue),
    rate_limiter: RateLimiter = Depends(build_rate_limiter),
    identifier: str = Depends(_rate_limit_key),
) -> AlertCreateResponse:
    # Rate limit: 10 alerts/minute per API key (or client IP if unauthenticated)
    if not rate_limiter.is_allowed("alerts", identifier):
        raise TooManyRequestsError(
            "Too many alerts — rate limit exceeded (10/min)",
            details={"scope": "alerts", "identifier": identifier},
        )
    return AlertService(db, settings, enqueue_diagnosis, enqueue_notification).create_alert(payload)
