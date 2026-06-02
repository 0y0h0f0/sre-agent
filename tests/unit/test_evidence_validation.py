"""Unit tests for evidence cross-validation (roadmap Phase 1.3)."""

from __future__ import annotations

from typing import Any

from packages.agent.evidence_validation import (
    ANOMALY,
    NORMAL,
    Signal,
    apply_confidence_adjustment,
    cross_validate,
    cross_validate_state,
    derive_signals,
)


def _ev(payload: dict[str, Any]) -> dict[str, Any]:
    return {"type": "x", "source": "s", "payload": payload}


# --------------------------------------------------------------------------- #
# Fusion logic                                                                 #
# --------------------------------------------------------------------------- #
class TestCrossValidate:
    def test_insufficient_when_no_signals(self) -> None:
        result = cross_validate([], degraded_sources=["metrics", "logs"])
        assert result["status"] == "insufficient"
        assert result["confidence_adjustment"] == 0.0
        assert result["needs_human_review"] is False
        assert result["degraded_sources"] == ["metrics", "logs"]

    def test_corroboration_raises_confidence(self) -> None:
        signals = [
            Signal("traces", ANOMALY, 1.0),
            Signal("metrics", ANOMALY, 0.8),
        ]
        result = cross_validate(signals)
        assert result["status"] == "corroborated"
        assert result["confidence_adjustment"] > 0
        assert result["needs_human_review"] is False
        assert set(result["corroborating_sources"]) == {"traces", "metrics"}

    def test_corroboration_beats_single_source(self) -> None:
        single = cross_validate([Signal("metrics", ANOMALY, 0.8)])
        multi = cross_validate(
            [Signal("metrics", ANOMALY, 0.8), Signal("logs", ANOMALY, 0.6)]
        )
        # Acceptance: multi-source agreement yields higher confidence than single.
        assert single["status"] == "single_source"
        assert single["confidence_adjustment"] == 0.0
        assert multi["confidence_adjustment"] > single["confidence_adjustment"]

    def test_conflict_flags_human_review(self) -> None:
        signals = [
            Signal("metrics", ANOMALY, 0.8),
            Signal("logs", NORMAL, 0.6),
        ]
        result = cross_validate(signals)
        assert result["status"] == "conflicting"
        assert result["needs_human_review"] is True
        assert result["confidence_adjustment"] < 0
        assert result["corroborating_sources"] == ["metrics"]
        assert result["healthy_sources"] == ["logs"]

    def test_all_healthy_is_not_conflict(self) -> None:
        result = cross_validate([Signal("metrics", NORMAL, 0.8)])
        assert result["status"] == "no_anomaly"
        assert result["needs_human_review"] is False

    def test_corroboration_bonus_is_capped(self) -> None:
        signals = [
            Signal("traces", ANOMALY, 1.0),
            Signal("metrics", ANOMALY, 0.8),
            Signal("logs", ANOMALY, 0.6),
            Signal("deployment", ANOMALY, 0.4),
        ]
        result = cross_validate(signals)
        assert result["confidence_adjustment"] == 0.15


class TestApplyAdjustment:
    def test_clamps_to_ceiling(self) -> None:
        assert apply_confidence_adjustment(0.95, 0.15) == 0.99

    def test_clamps_to_floor(self) -> None:
        assert apply_confidence_adjustment(0.1, -0.5) == 0.05

    def test_normal_adjustment(self) -> None:
        assert apply_confidence_adjustment(0.8, 0.1) == 0.9


# --------------------------------------------------------------------------- #
# Signal derivation from evidence payloads                                     #
# --------------------------------------------------------------------------- #
class TestDeriveSignals:
    def test_empty_sources_are_degraded(self) -> None:
        signals, degraded = derive_signals({})
        assert signals == []
        assert set(degraded) == {"metrics", "logs", "traces", "deployment", "k8s", "db"}

    def test_fallback_items_without_payload_are_degraded(self) -> None:
        state = {"metrics_evidence": [{"type": "metric", "status": "degraded"}]}
        _, degraded = derive_signals(state)
        assert "metrics" in degraded

    def test_metric_change_ratio_is_anomaly(self) -> None:
        state = {"metrics_evidence": [_ev({"stats": {"change_ratio": 0.8}})]}
        signals, _ = derive_signals(state)
        assert signals == [Signal("metrics", ANOMALY, 0.8)]

    def test_metric_stable_is_normal(self) -> None:
        state = {"metrics_evidence": [_ev({"stats": {"change_ratio": 0.05}})]}
        signals, _ = derive_signals(state)
        assert signals[0].direction == NORMAL

    def test_metric_without_stats_is_neutral(self) -> None:
        state = {"metrics_evidence": [_ev({"query": "up"})]}
        signals, degraded = derive_signals(state)
        assert signals == []
        assert "metrics" not in degraded  # present but neutral, not degraded

    def test_log_error_type_is_anomaly(self) -> None:
        state = {"logs_evidence": [_ev({"top_error_type": "connection_error", "sample_count": 5})]}
        signals, _ = derive_signals(state)
        assert signals[0] == Signal("logs", ANOMALY, 0.6)

    def test_log_benign_is_normal(self) -> None:
        state = {"logs_evidence": [_ev({"top_error_type": "info", "sample_count": 5})]}
        signals, _ = derive_signals(state)
        assert signals[0].direction == NORMAL

    def test_trace_errors_is_anomaly(self) -> None:
        state = {"traces_evidence": [_ev({"spans": 10, "errors": 3})]}
        signals, _ = derive_signals(state)
        assert signals[0] == Signal("traces", ANOMALY, 1.0)

    def test_trace_clean_is_normal(self) -> None:
        state = {"traces_evidence": [_ev({"spans": 10, "errors": 0})]}
        signals, _ = derive_signals(state)
        assert signals[0].direction == NORMAL

    def test_deployment_change_is_anomaly(self) -> None:
        state = {"deployment_evidence": [_ev({"change_count": 1, "changes": [{"sha": "x"}]})]}
        signals, _ = derive_signals(state)
        assert signals[0] == Signal("deployment", ANOMALY, 0.4)

    def test_deployment_no_change_is_neutral_not_healthy(self) -> None:
        # No deploy must not become a "healthy" dissent that creates false conflict.
        state = {"deployment_evidence": [_ev({"change_count": 0})]}
        signals, _ = derive_signals(state)
        assert signals == []


# --------------------------------------------------------------------------- #
# End-to-end on a realistic multi-source state                                 #
# --------------------------------------------------------------------------- #
class TestCrossValidateState:
    def test_corroborated_incident(self) -> None:
        state = {
            "metrics_evidence": [_ev({"stats": {"change_ratio": 0.9}})],
            "logs_evidence": [_ev({"top_error_type": "5xx_error", "sample_count": 20})],
            "traces_evidence": [_ev({"spans": 30, "errors": 5})],
            "deployment_evidence": [_ev({"change_count": 1})],
        }
        result = cross_validate_state(state)
        assert result["status"] == "corroborated"
        assert result["needs_human_review"] is False
        assert result["confidence_adjustment"] > 0

    def test_db_exhaustion_without_deploy_is_not_conflict(self) -> None:
        # Metrics + logs + traces anomalous, no deploy → corroborated, not conflict.
        state = {
            "metrics_evidence": [_ev({"stats": {"change_ratio": 0.7}})],
            "logs_evidence": [_ev({"top_error_type": "connection_error", "sample_count": 8})],
            "traces_evidence": [_ev({"spans": 12, "errors": 2})],
            "deployment_evidence": [_ev({"change_count": 0})],
        }
        result = cross_validate_state(state)
        assert result["status"] == "corroborated"
        assert result["needs_human_review"] is False

    def test_conflict_with_degraded_source(self) -> None:
        state = {
            "metrics_evidence": [_ev({"stats": {"change_ratio": 0.9}})],
            "logs_evidence": [_ev({"top_error_type": "info", "sample_count": 5})],
            "traces_evidence": [{"type": "trace", "status": "degraded"}],
        }
        result = cross_validate_state(state)
        assert result["status"] == "conflicting"
        assert result["needs_human_review"] is True
        assert "traces" in result["degraded_sources"]
        assert "deployment" in result["degraded_sources"]
