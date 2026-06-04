from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from apps.api.dependencies import (
    NotificationTaskEnqueue,
    TaskEnqueue,
    get_app_settings,
    get_db,
    get_notification_task_enqueue,
    get_task_enqueue,
)
from apps.api.schemas.alerts import AlertCreateRequest, AlertCreateResponse
from apps.api.services.alert_service import AlertService
from packages.common.settings import Settings

router = APIRouter(prefix="/api/alerts", tags=["alerts"])


@router.post("", response_model=AlertCreateResponse, status_code=202)
def create_alert(
    payload: AlertCreateRequest,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_app_settings),
    enqueue_diagnosis: TaskEnqueue = Depends(get_task_enqueue),
    enqueue_notification: NotificationTaskEnqueue = Depends(get_notification_task_enqueue),
) -> AlertCreateResponse:
    return AlertService(db, settings, enqueue_diagnosis, enqueue_notification).create_alert(payload)
