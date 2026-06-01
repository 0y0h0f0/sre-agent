from __future__ import annotations

import json
from pathlib import Path

from packages.evals.datasets import load_suite_cases
from packages.evals.datasets.harness import run_case, run_suite


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
    assert payload["metrics"]["provider_prompt_cache_hit_rate"] == "unknown"
    assert payload["metrics"]["app_prompt_segment_cache_hit_rate"] == "unknown"
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
