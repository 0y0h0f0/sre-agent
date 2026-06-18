"""Integration tests for service-level transaction visibility."""

from __future__ import annotations

from collections.abc import Generator
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from apps.api.schemas.actions import ExecuteRequest
from apps.api.schemas.evals import EvalRunRequest, ReplayRunRequest, ShadowRunRequest
from apps.api.services.action_service import ActionService
from apps.api.services.eval_service import EvalService
from packages.common.errors import ApprovalRequiredError
from packages.db.base import Base
from packages.db.models import Action, AgentRun, EvalRun, Incident


@pytest.fixture()
def session_factory(tmp_path) -> Generator[sessionmaker[Session], None, None]:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'tx.sqlite3'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
    )
    try:
        yield factory
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


def _seed_incident_and_run(db: Session) -> tuple[str, str]:
    incident_id = "inc_tx"
    agent_run_id = "run_tx"
    db.add(
        Incident(
            incident_id=incident_id,
            fingerprint="fp-tx",
            source="mock",
            service="checkout-api",
            severity="P2",
            alert_name="TxTest",
            status="waiting_approval",
            starts_at=datetime(2026, 6, 1, tzinfo=UTC),
            labels={},
            annotations={},
        )
    )
    db.add(AgentRun(agent_run_id=agent_run_id, incident_id=incident_id))
    return incident_id, agent_run_id


def test_l4_action_block_is_committed_before_error(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as db:
        incident_id, agent_run_id = _seed_incident_and_run(db)
        db.add(
            Action(
                action_id="act_l4_tx",
                incident_id=incident_id,
                agent_run_id=agent_run_id,
                type="delete_data",
                risk_level="L4",
                status="proposed",
                executor="fixture",
                target="checkout-api",
                params={},
                reason="destructive test action",
            )
        )
        db.commit()

    with session_factory() as db:
        with pytest.raises(ApprovalRequiredError):
            ActionService(db).execute(
                "act_l4_tx",
                ExecuteRequest(operator="tester", reason="must remain blocked"),
            )

    with session_factory() as db:
        action = db.scalars(
            select(Action).where(Action.action_id == "act_l4_tx")
        ).one()
        assert action.status == "blocked"
        assert action.execution_result == {
            "status": "blocked",
            "message": "L4 destructive actions are permanently blocked",
        }


def test_eval_run_is_committed_before_enqueue(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[str] = []

    def fake_delay(
        eval_run_id: str,
        suite: str,
        model: str,
        prompt_version: str,
    ) -> None:
        with session_factory() as verify_db:
            persisted = verify_db.scalars(
                select(EvalRun).where(EvalRun.eval_run_id == eval_run_id)
            ).one_or_none()
            assert persisted is not None
            assert persisted.status == "queued"
        observed.append(eval_run_id)

    from apps.worker.eval_tasks import run_eval_suite_task

    monkeypatch.setattr(run_eval_suite_task, "delay", fake_delay)

    with session_factory() as db:
        response = EvalService(db).trigger_smoke_eval(
            EvalRunRequest(suite="smoke", model="fake-diagnosis-model")
        )

    assert observed == [response.eval_run_id]


def test_replay_eval_run_is_committed_before_enqueue(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[str] = []

    def fake_delay(
        eval_run_id: str,
        limit: int,
        service: str | None,
        incident_ids: list[str],
        model: str | None,
        prompt_version: str,
    ) -> None:
        with session_factory() as verify_db:
            persisted = verify_db.scalars(
                select(EvalRun).where(EvalRun.eval_run_id == eval_run_id)
            ).one_or_none()
            assert persisted is not None
            assert persisted.status == "queued"
            assert persisted.suite == "replay"
            assert persisted.metrics["limit"] == limit
            assert persisted.metrics["incident_ids"] == incident_ids
        observed.append(eval_run_id)

    from apps.worker.eval_tasks import run_replay_eval_task

    monkeypatch.setattr(run_replay_eval_task, "delay", fake_delay)

    with session_factory() as db:
        response = EvalService(db).trigger_replay(
            ReplayRunRequest(limit=2, incident_ids=["inc_a"], model="fake-diagnosis-model")
        )

    assert observed == [response.eval_run_id]


def test_shadow_eval_terminal_status_is_committed(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as db:
        response = EvalService(db).trigger_shadow(
            data=ShadowRunRequest(
                incident_id="inc_missing",
                shadow_model="fake-diagnosis-model",
                shadow_prompt_version="v1",
            )
        )

    with session_factory() as db:
        eval_run = db.scalars(
            select(EvalRun).where(EvalRun.eval_run_id == response.eval_run_id)
        ).one()
        assert eval_run.status == "shadow_failed"
        assert eval_run.metrics == {"error": "incident not found"}
