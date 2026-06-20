"""Evaluation service — triggers eval runs and shadow mode."""

from __future__ import annotations

from sqlalchemy.orm import Session

from apps.api.schemas.evals import (
    EvalRunDetail,
    EvalRunListResponse,
    EvalRunRequest,
    EvalRunResponse,
    ReplayRunRequest,
    ShadowRunRequest,
    ShadowRunResponse,
)
from packages.common.errors import NotFoundError
from packages.common.ids import new_id
from packages.db.models import EvalRun


class EvalService:
    def __init__(self, db: Session) -> None:
        self._db = db

    def trigger_smoke_eval(self, data: EvalRunRequest) -> EvalRunResponse:
        # Persist and commit the EvalRun before enqueueing. The Celery worker
        # runs in a separate transaction/session and must be able to see the row.
        eval_run = EvalRun(
            eval_run_id=new_id("eval_"),
            status="queued",
            suite=data.suite,
            model_name=data.model or "fake-diagnosis-model",
            prompt_version=data.prompt_version,
        )
        self._db.add(eval_run)
        self._db.flush()
        self._db.commit()

        try:
            from apps.worker.eval_tasks import run_eval_suite_task

            # The API only schedules the suite. The worker owns run execution and
            # writes final metrics so HTTP requests never run the harness inline.
            run_eval_suite_task.delay(
                str(eval_run.eval_run_id), data.suite,
                str(eval_run.model_name), str(eval_run.prompt_version),
            )
        except Exception:
            # Keep an inspectable EvalRun if the broker is down; callers can see
            # that the request was accepted but enqueue did not happen.
            eval_run.status = "enqueue_failed"
            self._db.commit()

        return EvalRunResponse(
            eval_run_id=eval_run.eval_run_id,
            status=eval_run.status,
            created_at=eval_run.created_at,
        )

    def list_runs(self) -> EvalRunListResponse:
        from sqlalchemy import select

        # A small recent window is enough for the console and avoids returning
        # large historical metric blobs by default.
        runs = list(
            self._db.scalars(
                select(EvalRun).order_by(EvalRun.created_at.desc()).limit(50)
            ).all()
        )
        return EvalRunListResponse(
            items=[EvalRunDetail.model_validate(r) for r in runs],
            total=len(runs),
        )

    def get_run(self, eval_run_id: str) -> EvalRunDetail:
        from sqlalchemy import select

        run = self._db.scalars(
            select(EvalRun).where(EvalRun.eval_run_id == eval_run_id)
        ).one_or_none()
        if run is None:
            raise NotFoundError("eval_run", eval_run_id)
        return EvalRunDetail.model_validate(run)

    def trigger_shadow(self, data: ShadowRunRequest) -> ShadowRunResponse:
        from packages.evals.shadow import run_shadow_diagnosis

        # Shadow mode is currently a safe stub that writes only eval tables; keep
        # this synchronous so the caller immediately gets the terminal stub state.
        eval_run = run_shadow_diagnosis(
            self._db,
            data.incident_id,
            data.shadow_model,
            data.shadow_prompt_version,
        )
        return ShadowRunResponse(
            eval_run_id=eval_run.eval_run_id,
            status=eval_run.status,
        )

    def trigger_replay(self, data: ReplayRunRequest) -> EvalRunResponse:
        # Replay is asynchronous because it can scan historical incidents and run
        # the graph in temporary databases. The real DB stores only this EvalRun.
        eval_run = EvalRun(
            eval_run_id=new_id("eval_"),
            status="queued",
            suite="replay",
            model_name=data.model or "fake-diagnosis-model",
            prompt_version=data.prompt_version,
            # Store request parameters up front so queued/failed replay requests
            # remain auditable even if Celery never starts the task.
            metrics={
                "limit": data.limit,
                "service": data.service,
                "incident_ids": data.incident_ids,
                "status": "queued",
            },
        )
        self._db.add(eval_run)
        self._db.flush()
        self._db.commit()

        try:
            from apps.worker.eval_tasks import run_replay_eval_task

            # Replay worker reads historical incidents but must only persist
            # aggregate metrics back to this EvalRun.
            run_replay_eval_task.delay(
                str(eval_run.eval_run_id),
                data.limit,
                data.service,
                list(data.incident_ids),
                data.model,
                str(data.prompt_version),
            )
        except Exception:
            eval_run.status = "enqueue_failed"
            self._db.commit()

        return EvalRunResponse(
            eval_run_id=eval_run.eval_run_id,
            status=eval_run.status,
            created_at=eval_run.created_at,
        )
