from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy.orm import Session

from packages.common.settings import Settings
from packages.db.models import AgentRun, Incident
from packages.evals.datasets import load_suite_cases
from packages.evals.datasets.harness import (
    EvalCaseResult,
    _build_deps,
    _eval_settings,
    _seed_runbooks,
    run_case,
    run_suite,
)
from packages.evals.replay import run_replay_suite, select_replay_targets
from packages.rag.embedding_factory import FakeEmbeddingProvider
from packages.rag.reranker_backends import FakeRerankerBackend
from packages.tools.cache import RequestLocalToolCache


def test_smoke_dataset_has_four_cases() -> None:
    cases = load_suite_cases("smoke")
    assert len(cases) == 4
    assert {case.expected["expected_risk_level"] for case in cases} == {"L1", "L2", "L3"}


def test_run_smoke_suite_writes_report(tmp_path: Path) -> None:
    output = tmp_path / "eval-smoke.json"
    report = run_suite("smoke", output=output)

    assert output.exists()
    assert output.with_suffix(".md").exists()

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["suite"] == "smoke"
    assert payload["dataset_version"] == "smoke-v1"
    assert payload["metrics"]["case_count"] == 4
    assert payload["metrics"]["root_cause_top1_hit_rate"] == 1.0
    assert payload["metrics"]["root_cause_top3_hit_rate"] == 1.0
    assert payload["metrics"]["required_evidence_coverage"] == 1.0
    assert payload["metrics"]["high_risk_interception_rate"] == 1.0
    assert payload["metrics"]["json_valid_rate"] == 1.0
    assert payload["metrics"]["report_generation_rate"] == 1.0
    assert payload["metrics"]["avg_prompt_token_estimate"] > 0
    assert isinstance(payload["metrics"]["provider_prompt_cache_hit_rate"], (int, float))
    assert isinstance(payload["metrics"]["app_prompt_segment_cache_hit_rate"], (int, float))
    assert payload["metrics"]["tool_success_rate"] >= 0.75
    assert payload["metrics"]["tool_cache_hit_rate"] >= 0.0
    assert len(payload["cases"]) == 4
    assert all(case["structured_output_valid"] for case in payload["cases"])
    assert all(case["report_id"] for case in payload["cases"])
    assert report.metrics["case_count"] == 4


def test_run_one_case_returns_report_and_evidence() -> None:
    case = load_suite_cases("smoke")[1]
    result = run_case(case, suite="smoke")

    assert result.root_cause_hit is True
    assert result.top3_hit is True
    assert result.required_evidence_hit is True
    assert result.status == "succeeded"
    assert result.report_id is not None


def test_eval_harness_forces_fake_rag_providers(
    monkeypatch: pytest.MonkeyPatch,
    db_session: Session,
) -> None:
    monkeypatch.setenv("EMBEDDING_PROVIDER", "bge_zh")
    monkeypatch.setenv("RERANKER_PROVIDER", "bge")
    settings = _eval_settings()

    assert settings.embedding_provider == "fake"
    assert settings.reranker_provider == "fake"

    _seed_runbooks(db_session, Path("demo/runbooks"))
    deps = _build_deps(
        db_session,
        settings,
        load_suite_cases("smoke")[0],
        "run_eval_provider_test",
        RequestLocalToolCache(),
    )

    retriever = cast(Any, deps.runbook_search_tool).retriever
    assert isinstance(retriever.embedding_provider, FakeEmbeddingProvider)
    assert isinstance(retriever._reranker, FakeRerankerBackend)


def test_replay_selects_only_incidents_with_historical_root_cause(
    db_session: Session,
) -> None:
    _add_historical_incident(db_session, "inc_replay_ok", with_root_cause=True)
    _add_historical_incident(db_session, "inc_replay_skip", with_root_cause=False)
    db_session.commit()

    targets, skipped = select_replay_targets(db_session, limit=10)

    assert [target.incident_id for target in targets] == ["inc_replay_ok"]
    assert skipped == [
        {"incident_id": "inc_replay_skip", "reason": "original_root_cause_missing"}
    ]


def test_run_replay_suite_reports_drift_without_running_real_actions(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _add_historical_incident(db_session, "inc_replay_consistent", with_root_cause=True)
    _add_historical_incident(db_session, "inc_replay_drifted", with_root_cause=True)
    db_session.commit()

    def fake_run_target(
        source_db: Session,
        target: Any,
        settings: Settings,
        *,
        prompt_version: str,
        model: str | None,
    ) -> EvalCaseResult:
        hit = target.incident_id == "inc_replay_consistent"
        return EvalCaseResult(
            case_id=target.incident_id,
            incident_type=target.alert_name,
            source_path=f"replay:{target.incident_id}",
            incident_id=target.incident_id,
            agent_run_id=f"run_{target.incident_id}",
            status="succeeded",
            approval_interrupted=False,
            root_cause_summary=target.original_summary if hit else "different root cause",
            root_cause_hit=hit,
            top3_hit=hit,
            required_evidence_hit=True,
            expected_risk_level="historical",
            actual_risk_level="L1",
            duration_ms=10,
            tool_total=1,
            tool_successes=1,
            tool_cache_hits=0,
            prompt_token_estimate=100,
            completion_token_estimate=0,
            compression_retention_rate=1.0,
            structured_output_valid=True,
            memory_misuse=False,
            report_id="rpt_replay",
            report_version=1,
            error=None,
        )

    monkeypatch.setattr("packages.evals.replay._run_replay_target", fake_run_target)

    report = run_replay_suite(db_session, Settings(), limit=10)

    assert report.suite == "replay"
    assert report.metrics["case_count"] == 2
    assert report.metrics["selected_count"] == 2
    assert report.metrics["root_cause_consistency_rate"] == 0.5
    assert report.metrics["replay_drift_count"] == 1
    assert report.metrics["drifted_case_ids"] == ["inc_replay_drifted"]
    assert report.metrics["fixture_executor_forced"] is True
    assert "temp_db" in report.metrics["write_scope"]


def _add_historical_incident(
    db_session: Session,
    incident_id: str,
    *,
    with_root_cause: bool,
) -> None:
    now = datetime(2026, 6, 1, 0, 0, tzinfo=UTC)
    db_session.add(
        Incident(
            incident_id=incident_id,
            fingerprint=f"fp_{incident_id}",
            source="mock",
            service="checkout-api",
            severity="P2",
            alert_name="High5xxAfterDeploy",
            status="resolved",
            starts_at=now,
            labels={},
            annotations={"summary": "5xx increased after deploy"},
            raw_payload={},
            root_cause_summary="bad deployment caused high 5xx",
            created_at=now,
        )
    )
    db_session.add(
        AgentRun(
            agent_run_id=f"run_{incident_id}",
            incident_id=incident_id,
            status="succeeded",
            model_name="fake-diagnosis-model",
            prompt_version="v1",
            state={
                "root_cause": {"summary": "bad deployment caused high 5xx"}
                if with_root_cause
                else {}
            },
            checkpoint_thread_id=f"run_{incident_id}",
            checkpoint_ns="",
        )
    )
