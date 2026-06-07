"""Celery tasks for evaluation runs."""

from __future__ import annotations

from typing import Any

from apps.worker.celery_app import celery_app
from packages.common.time import utc_now
from packages.db.session import SessionLocal


@celery_app.task(bind=True, max_retries=2)  # type: ignore[untyped-decorator]
def run_eval_suite_task(
    self: Any,
    eval_run_id: str,
    suite: str,
    model: str,
    prompt_version: str,
) -> dict[str, Any]:
    """Run an eval suite and persist results to the database."""
    with SessionLocal() as db:
        from sqlalchemy import select

        from packages.db.models import EvalRun

        eval_run = db.scalars(
            select(EvalRun).where(EvalRun.eval_run_id == eval_run_id)
        ).one_or_none()

        if eval_run is None:
            return {"error": "eval run not found", "eval_run_id": eval_run_id}

        try:
            eval_run.status = "running"
            eval_run.started_at = utc_now()
            db.commit()

            from packages.evals.datasets.harness import run_suite

            report = run_suite(suite)

            eval_run.status = "succeeded"
            eval_run.metrics = report.metrics
            eval_run.finished_at = utc_now()
            db.commit()

            return {
                "eval_run_id": eval_run_id,
                "status": "succeeded",
                "metrics": report.metrics,
            }
        except Exception as exc:
            eval_run.status = "failed"
            eval_run.metrics = {"error": str(exc)}
            eval_run.finished_at = utc_now()
            db.commit()
            return {"eval_run_id": eval_run_id, "status": "failed", "error": str(exc)}
