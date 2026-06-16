from __future__ import annotations

from apps.worker.celery_app import _is_celery_worker_process


def test_metrics_server_starts_only_for_worker_command() -> None:
    assert _is_celery_worker_process(
        ["celery", "-A", "apps.worker.tasks:celery_app", "worker", "--loglevel=INFO"]
    )


def test_metrics_server_does_not_start_for_inspect_probe() -> None:
    assert not _is_celery_worker_process(
        [
            "celery",
            "-A",
            "apps.worker.tasks:celery_app",
            "inspect",
            "ping",
            "-d",
            "celery@worker-0",
        ]
    )


def test_metrics_server_does_not_start_for_beat_command() -> None:
    assert not _is_celery_worker_process(
        ["celery", "-A", "apps.worker.tasks:celery_app", "beat", "--loglevel=INFO"]
    )
