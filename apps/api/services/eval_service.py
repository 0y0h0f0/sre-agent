"""Evaluation service — triggers eval runs and shadow mode."""

from __future__ import annotations

from sqlalchemy.orm import Session

from apps.api.schemas.evals import (
    EvalRunDetail,
    EvalRunListResponse,
    EvalRunRequest,
    EvalRunResponse,
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

            run_eval_suite_task.delay(
                str(eval_run.eval_run_id), data.suite,
                str(eval_run.model_name), str(eval_run.prompt_version),
            )
        except Exception:
            eval_run.status = "enqueue_failed"
            self._db.commit()

        return EvalRunResponse(
            eval_run_id=eval_run.eval_run_id,
            status=eval_run.status,
            created_at=eval_run.created_at,
        )

    def list_runs(self) -> EvalRunListResponse:
        from sqlalchemy import select

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
