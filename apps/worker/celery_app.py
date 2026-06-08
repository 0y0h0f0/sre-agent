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
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_time_limit=600,      # 10 min — real LLM diagnosis can take 60-120s
    task_soft_time_limit=300,  # 5 min — grace period before hard kill
    task_default_retry_delay=5,
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    task_always_eager=settings.celery_task_always_eager,
    timezone=settings.notification_timezone,
    broker_connection_retry_on_startup=True,
    broker_pool_limit=10,
    result_expires=86400,
    beat_schedule={
        "daily-incident-summary": {
            "task": "apps.worker.tasks.send_daily_incident_summary",
            "schedule": crontab(hour=9, minute=0),
        },
        "auto-approve-stale-approvals": {
            "task": "apps.worker.tasks.auto_approve_stale_approvals",
            "schedule": 60.0,
        },
    },
)

# Start Prometheus metrics HTTP server for worker scraping (Phase 7.2)
if (
    settings.prometheus_metrics_enabled
    and not settings.celery_task_always_eager
):
    try:
        import prometheus_client

        prometheus_client.start_http_server(settings.celery_metrics_port)
    except Exception:
        import logging
        logging.getLogger(__name__).warning(
            "Failed to start Prometheus metrics server on port %d",
            settings.celery_metrics_port, exc_info=True,
        )
