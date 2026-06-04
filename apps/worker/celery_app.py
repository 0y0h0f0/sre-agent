from __future__ import annotations

from celery import Celery
from celery.schedules import crontab

from packages.common.settings import get_settings

settings = get_settings()

celery_app = Celery(
    "sre_incident_response_agent",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
)
celery_app.conf.update(
    task_track_started=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    task_time_limit=120,
    task_soft_time_limit=90,
    task_default_retry_delay=5,
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    task_always_eager=settings.celery_task_always_eager,
    timezone=settings.notification_timezone,
    beat_schedule={
        "daily-incident-summary": {
            "task": "apps.worker.tasks.send_daily_incident_summary",
            "schedule": crontab(hour=9, minute=0),
        },
    },
)
