from __future__ import annotations

import os
from collections.abc import Generator
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from apps.api.dependencies import (
    get_app_settings,
    get_db,
    get_notification_task_enqueue,
    get_resume_task_enqueue,
    get_task_enqueue,
)
from apps.api.main import create_app
from packages.common.settings import Settings, get_settings


@pytest.fixture(autouse=True)
def _disable_auth_in_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disable API key auth for all tests."""
    monkeypatch.setenv("API_KEY_AUTH_ENABLED", "false")
    get_settings.cache_clear()
from packages.db import models  # noqa: F401
from packages.db.base import Base


class FakeEnqueue:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def __call__(self, incident_id: str, agent_run_id: str) -> str:
        self.calls.append((incident_id, agent_run_id))
        return f"task-{len(self.calls)}"


class FakeResumeEnqueue:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def __call__(self, agent_run_id: str, decision: str) -> str:
        self.calls.append((agent_run_id, decision))
        return f"resume-task-{len(self.calls)}"


class FakeNotificationEnqueue:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, notification_type: str, payload: dict) -> str:
        self.calls.append((notification_type, payload))
        return f"email-task-{len(self.calls)}"


@pytest.fixture()
def db_session() -> Generator[Session, None, None]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(
        bind=engine, autoflush=False, autocommit=False, expire_on_commit=False
    )
    with SessionLocal() as session:
        yield session
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture()
def fake_enqueue() -> FakeEnqueue:
    return FakeEnqueue()


@pytest.fixture()
def fake_resume_enqueue() -> FakeResumeEnqueue:
    return FakeResumeEnqueue()


@pytest.fixture()
def fake_notification_enqueue() -> FakeNotificationEnqueue:
    return FakeNotificationEnqueue()


@pytest.fixture()
def test_settings() -> Settings:
    return Settings(
        database_url="sqlite+pysqlite:///:memory:",
        redis_url="memory://redis",
        celery_broker_url="memory://broker",
        celery_result_backend="memory://backend",
        api_key_auth_enabled=False,
        celery_task_always_eager=True,
    )


@pytest.fixture()
def client(
    db_session: Session,
    fake_enqueue: FakeEnqueue,
    fake_resume_enqueue: FakeResumeEnqueue,
    fake_notification_enqueue: FakeNotificationEnqueue,
    test_settings: Settings,
) -> Generator[TestClient, None, None]:
    # Clear settings cache so middleware picks up test overrides
    get_settings.cache_clear()
    app = create_app()

    def override_db() -> Generator[Session, None, None]:
        yield db_session

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_task_enqueue] = lambda: fake_enqueue
    app.dependency_overrides[get_resume_task_enqueue] = lambda: fake_resume_enqueue
    app.dependency_overrides[get_notification_task_enqueue] = lambda: fake_notification_enqueue
    app.dependency_overrides[get_app_settings] = lambda: test_settings
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
    get_settings.cache_clear()


@pytest.fixture()
def alert_payload() -> dict:
    return {
        "source": "mock",
        "fingerprint": "fp-checkout-5xx",
        "service": "checkout",
        "severity": "P2",
        "alert_name": "High5xxAfterDeploy",
        "starts_at": datetime(2026, 6, 1, 0, 0, tzinfo=UTC).isoformat(),
        "labels": {"team": "payments"},
        "annotations": {"summary": "5xx increased after deploy"},
    }
