from __future__ import annotations

from collections.abc import Callable, Generator

from sqlalchemy.orm import Session

from packages.common.settings import Settings, get_settings
from packages.db.session import get_session

TaskEnqueue = Callable[[str, str], str]


def get_db() -> Generator[Session, None, None]:
    yield from get_session()


def get_app_settings() -> Settings:
    return get_settings()


ResumeTaskEnqueue = Callable[[str, str], str]


def get_task_enqueue() -> TaskEnqueue:
    from apps.worker.tasks import enqueue_diagnosis_task

    return enqueue_diagnosis_task


def get_resume_task_enqueue() -> ResumeTaskEnqueue:
    from apps.worker.tasks import enqueue_resume_task

    return enqueue_resume_task
