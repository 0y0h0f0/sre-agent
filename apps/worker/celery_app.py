from __future__ import annotations

import sys
from collections.abc import Sequence
from typing import Any

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
    imports=("apps.worker.tasks", "apps.worker.eval_tasks"),
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
        "poll-alertmanager": {
            "task": "apps.worker.tasks.poll_alertmanager",
            "schedule": settings.alert_poll_interval_seconds,
        },
        "periodic-discovery": {
            "task": "apps.worker.tasks.auto_discovery_rerun",
            "schedule": crontab(minute="*/30"),  # every 30 minutes
        },
    },
)

# Trigger auto-discovery on worker startup (initial scan, then Beat handles periodic).
@celery_app.on_after_finalize.connect  # type: ignore[untyped-decorator]
def _trigger_startup_discovery(sender: Any, **kwargs: Any) -> None:
    """Enqueue an auto-discovery run when the worker first starts."""
    try:
        from apps.worker.tasks import auto_discovery_rerun
        auto_discovery_rerun.delay()
    except Exception:
        import logging
        logging.getLogger(__name__).warning(
            "Failed to enqueue startup discovery", exc_info=True
        )


def _is_celery_worker_process(argv: Sequence[str] | None = None) -> bool:
    """Return True only for the long-running Celery worker process.

    Celery control commands such as ``celery inspect ping`` import this module
    too. Those short-lived probe processes must not start the worker metrics
    HTTP server because the real worker already owns the port.
    """
    args = list(sys.argv if argv is None else argv)
    return "worker" in args


# Start Prometheus metrics HTTP server for worker scraping (Phase 7.2).
if (
    settings.prometheus_metrics_enabled
    and not settings.celery_task_always_eager
    and _is_celery_worker_process()
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
