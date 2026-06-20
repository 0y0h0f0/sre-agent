"""Celery tasks for evaluation runs.

Eval tasks persist their own status/metrics and do not run remediation paths.
CI smoke evals should remain FakeLLM/deterministic; manual replay/full evals can
be triggered separately without becoming stable CI gates.
"""

from __future__ import annotations

from typing import Any

from apps.worker.celery_app import celery_app
from packages.common.settings import get_settings
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
            # Mark running before executing the suite so API clients can see
            # progress even if the worker later fails.
            eval_run.status = "running"
            eval_run.started_at = utc_now()
            db.commit()

            from packages.evals.datasets.harness import run_suite

            # run_suite is deterministic for smoke datasets; model/prompt fields
            # are tracked on the EvalRun row created by the API.
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
            # Persist the error into metrics and return a failed result instead
            # of letting the task disappear as an unstructured worker exception.
            eval_run.status = "failed"
            eval_run.metrics = {"error": str(exc)}
            eval_run.finished_at = utc_now()
            db.commit()
            return {"eval_run_id": eval_run_id, "status": "failed", "error": str(exc)}


@celery_app.task(bind=True, max_retries=2)  # type: ignore[untyped-decorator]
def run_replay_eval_task(
    self: Any,
    eval_run_id: str,
    limit: int,
    service: str | None,
    incident_ids: list[str],
    model: str | None,
    prompt_version: str,
) -> dict[str, Any]:
    """Run a safe historical replay eval and persist summary metrics."""
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

            from packages.evals.datasets.harness import asdict_case
            from packages.evals.replay import run_replay_suite

            # Replay reads historical incident data and produces evaluation
            # metrics/cases. It must not execute remediation or mutate incidents.
            report = run_replay_suite(
                db,
                get_settings(),
                limit=limit,
                service=service,
                incident_ids=incident_ids,
                model=model,
                prompt_version=prompt_version,
            )
            metrics = dict(report.metrics)
            metrics["cases"] = [asdict_case(case) for case in report.cases]

            eval_run.status = "succeeded"
            eval_run.metrics = metrics
            eval_run.model_name = report.model_name
            eval_run.prompt_version = report.prompt_version
            eval_run.git_commit = report.git_commit
            eval_run.finished_at = utc_now()
            db.commit()

            return {
                "eval_run_id": eval_run_id,
                "status": "succeeded",
                "metrics": metrics,
            }
        except Exception as exc:
            # Keep failure visible through the eval API with suite_type context.
            eval_run.status = "failed"
            eval_run.metrics = {"error": str(exc), "suite_type": "historical_replay"}
            eval_run.finished_at = utc_now()
            db.commit()
            return {"eval_run_id": eval_run_id, "status": "failed", "error": str(exc)}
