from __future__ import annotations

from apps.api.dependencies import get_app_settings, get_db, get_task_enqueue
from packages.common.settings import Settings


def test_get_db_returns_generator() -> None:
    gen = get_db()
    session = next(gen)
    try:
        assert session is not None
    finally:
        try:
            next(gen)
        except StopIteration:
            pass


def test_get_app_settings_returns_settings() -> None:
    result = get_app_settings()
    assert isinstance(result, Settings)


def test_get_task_enqueue_returns_callable() -> None:
    fn = get_task_enqueue()
    assert callable(fn)
    assert fn.__name__ == "enqueue_diagnosis_task"
