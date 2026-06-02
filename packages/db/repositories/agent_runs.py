from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from apps.api.schemas.common import AgentRunStatus
from packages.common.time import utc_now
from packages.db.models import AgentRun, AgentRunNode

TERMINAL_RUN_STATUSES = (
    AgentRunStatus.SUCCEEDED.value,
    AgentRunStatus.FAILED.value,
    AgentRunStatus.CANCELLED.value,
)
ACTIVE_RUN_STATUSES = (
    AgentRunStatus.QUEUED.value,
    AgentRunStatus.RUNNING.value,
    AgentRunStatus.WAITING_APPROVAL.value,
)


class AgentRunRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def create(self, agent_run_id: str, incident_id: str, *, model_name: str) -> AgentRun:
        run = AgentRun(
            agent_run_id=agent_run_id,
            incident_id=incident_id,
            status=AgentRunStatus.QUEUED.value,
            model_name=model_name,
            prompt_version="v1",
            state={},
            checkpoint_thread_id=agent_run_id,
            checkpoint_ns="",
        )
        self.db.add(run)
        return run

    def get_by_public_id(self, agent_run_id: str) -> AgentRun | None:
        stmt = select(AgentRun).where(AgentRun.agent_run_id == agent_run_id)
        return self.db.scalar(stmt)

    def get_for_update(self, agent_run_id: str) -> AgentRun | None:
        """Fetch a run with a row-level lock to serialize concurrent workers.

        Celery delivers tasks at-least-once, so two workers can pick up the same
        run. ``SELECT ... FOR UPDATE`` makes the status check + state transition
        atomic: the loser blocks until the winner commits, then observes the
        already-advanced status and short-circuits. On sqlite (tests) the lock
        clause is a harmless no-op.
        """
        stmt = select(AgentRun).where(AgentRun.agent_run_id == agent_run_id).with_for_update()
        return self.db.scalar(stmt)

    def get_latest_for_incident(self, incident_id: str) -> AgentRun | None:
        stmt = (
            select(AgentRun)
            .where(AgentRun.incident_id == incident_id)
            .order_by(AgentRun.created_at.desc(), AgentRun.id.desc())
            .limit(1)
        )
        return self.db.scalar(stmt)

    def get_active_for_incident(self, incident_id: str) -> AgentRun | None:
        stmt = (
            select(AgentRun)
            .where(
                AgentRun.incident_id == incident_id,
                AgentRun.status.in_(ACTIVE_RUN_STATUSES),
            )
            .order_by(AgentRun.created_at.desc(), AgentRun.id.desc())
            .limit(1)
        )
        return self.db.scalar(stmt)

    def list_for_incident(self, incident_id: str) -> Sequence[AgentRun]:
        stmt = (
            select(AgentRun)
            .where(AgentRun.incident_id == incident_id)
            .order_by(AgentRun.created_at.desc(), AgentRun.id.desc())
        )
        return self.db.scalars(stmt).all()

    def list_nodes(self, agent_run_id: str) -> Sequence[AgentRunNode]:
        stmt = (
            select(AgentRunNode)
            .where(AgentRunNode.agent_run_id == agent_run_id)
            .order_by(AgentRunNode.created_at.asc(), AgentRunNode.id.asc())
        )
        return self.db.scalars(stmt).all()

    def set_task_id(self, agent_run_id: str, celery_task_id: str) -> AgentRun:
        run = self.get_by_public_id(agent_run_id)
        if run is None:
            msg = f"agent run {agent_run_id} not found"
            raise ValueError(msg)
        run.celery_task_id = celery_task_id
        return run

    def mark_enqueue_failed(self, agent_run_id: str, message: str) -> AgentRun:
        run = self.get_by_public_id(agent_run_id)
        if run is None:
            msg = f"agent run {agent_run_id} not found"
            raise ValueError(msg)
        run.status = AgentRunStatus.FAILED.value
        run.error_code = "CELERY_ENQUEUE_FAILED"
        run.error_message = message
        run.finished_at = utc_now()
        return run

    def mark_running(self, run: AgentRun) -> AgentRun:
        run.status = AgentRunStatus.RUNNING.value
        run.started_at = run.started_at or utc_now()
        return run

    def mark_succeeded(self, run: AgentRun, state: dict[str, Any]) -> AgentRun:
        finished_at = utc_now()
        run.status = AgentRunStatus.SUCCEEDED.value
        run.finished_at = finished_at
        run.duration_ms = _duration_ms(run.started_at, finished_at)
        run.state = state
        return run

    def mark_failed(self, agent_run_id: str, error_code: str, error_message: str) -> AgentRun:
        run = self.get_by_public_id(agent_run_id)
        if run is None:
            msg = f"agent run {agent_run_id} not found"
            raise ValueError(msg)
        finished_at = utc_now()
        run.status = AgentRunStatus.FAILED.value
        run.finished_at = finished_at
        run.error_code = error_code
        run.error_message = error_message
        run.duration_ms = _duration_ms(run.started_at, finished_at)
        return run


def _duration_ms(started_at: datetime | None, finished_at: datetime) -> int | None:
    if started_at is None:
        return None
    return max(0, int((finished_at - started_at).total_seconds() * 1000))
