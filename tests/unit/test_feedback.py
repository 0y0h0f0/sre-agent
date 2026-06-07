"""Unit tests for Phase 5 feedback repositories and service (NFA, corrections, correlations)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import Session

from apps.api.schemas.feedback import (
    ActionCorrectionRequest,
    NfaMarkRequest,
    RootCauseCorrectionRequest,
)
from apps.api.services.feedback_service import FeedbackService
from packages.common.settings import Settings
from packages.db.models import Incident
from packages.db.repositories.false_positive_patterns import FalsePositivePatternRepository
from packages.db.repositories.feedback import FeedbackItemRepository
from packages.db.repositories.incident_correlations import IncidentCorrelationRepository
from packages.db.repositories.incidents import IncidentRepository


def _create_incident(
    db: Session,
    incident_id: str = "inc_test1",
    fingerprint: str = "fp-test",
    service: str = "checkout",
    alert_name: str = "TestAlert",
    severity: str = "P2",
    root_cause: str | None = "CPU saturation in checkout pod",
    status: str = "open",
) -> Incident:
    incident = Incident(
        incident_id=incident_id,
        fingerprint=fingerprint,
        source="mock",
        service=service,
        severity=severity,
        alert_name=alert_name,
        status=status,
        starts_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
        root_cause_summary=root_cause,
        labels={},
        annotations={},
        raw_payload={},
    )
    db.add(incident)
    return incident


def _settings() -> Settings:
    return Settings(
        database_url="sqlite+pysqlite:///:memory:",
        nfa_auto_suppress_threshold=3,
        nfa_reset_days=30,
    )


# ---------------------------------------------------------------------------
# FalsePositivePatternRepository
# ---------------------------------------------------------------------------


class TestFalsePositivePatternRepository:
    def test_increment_nfa_creates_new_pattern(self, db_session: Session) -> None:
        db_session.commit()
        repo = FalsePositivePatternRepository(db_session)
        pattern = repo.increment_nfa("fp-test", "checkout", "TestAlert")
        db_session.commit()

        assert pattern is not None
        assert pattern.nfa_count == 1
        assert pattern.status == "active"
        assert pattern.fingerprint == "fp-test"

    def test_increment_nfa_accumulates_count(self, db_session: Session) -> None:
        db_session.commit()
        repo = FalsePositivePatternRepository(db_session)
        repo.increment_nfa("fp-test", "checkout", "TestAlert")
        repo.increment_nfa("fp-test", "checkout", "TestAlert")
        result = repo.increment_nfa("fp-test", "checkout", "TestAlert")
        db_session.commit()

        assert result.nfa_count == 3
        assert result.status == "suppressed"

    def test_increment_nfa_auto_suppresses_at_threshold(self, db_session: Session) -> None:
        db_session.commit()
        repo = FalsePositivePatternRepository(db_session)
        repo.increment_nfa("fp-test", "checkout", "TestAlert")
        repo.increment_nfa("fp-test", "checkout", "TestAlert")
        result = repo.increment_nfa("fp-test", "checkout", "TestAlert")
        db_session.commit()

        assert result.nfa_count == 3
        assert result.status == "suppressed"
        assert result.suppressed_by == "auto"
        assert result.suppressed_at is not None

    def test_increment_nfa_respects_custom_threshold(self, db_session: Session) -> None:
        db_session.commit()
        repo = FalsePositivePatternRepository(db_session)
        repo.increment_nfa("fp-test", "checkout", "TestAlert", threshold=5)
        result = repo.increment_nfa("fp-test", "checkout", "TestAlert", threshold=5)
        db_session.commit()

        assert result.nfa_count == 2
        assert result.status == "active"

    def test_should_suppress_returns_true_when_suppressed(self, db_session: Session) -> None:
        db_session.commit()
        repo = FalsePositivePatternRepository(db_session)
        repo.increment_nfa("fp-test", "checkout", "TestAlert")
        repo.increment_nfa("fp-test", "checkout", "TestAlert")
        repo.increment_nfa("fp-test", "checkout", "TestAlert")
        db_session.commit()

        assert repo.should_suppress("fp-test") is True

    def test_should_suppress_returns_false_for_unknown(self, db_session: Session) -> None:
        db_session.commit()
        repo = FalsePositivePatternRepository(db_session)
        assert repo.should_suppress("nonexistent") is False

    def test_restore_pattern_resets_count(self, db_session: Session) -> None:
        db_session.commit()
        repo = FalsePositivePatternRepository(db_session)
        repo.increment_nfa("fp-test", "checkout", "TestAlert")
        repo.increment_nfa("fp-test", "checkout", "TestAlert")
        result = repo.increment_nfa("fp-test", "checkout", "TestAlert")
        db_session.commit()

        assert result.status == "suppressed"

        restored = repo.restore_pattern(result.pattern_id, "sre-oncall")
        db_session.commit()

        assert restored is not None
        assert restored.status == "active"
        assert restored.nfa_count == 0
        assert restored.restored_by == "sre-oncall"

    def test_expire_stale_patterns_resets_old_entries(self, db_session: Session) -> None:
        db_session.commit()
        repo = FalsePositivePatternRepository(db_session)
        pattern = repo.increment_nfa("fp-stale", "checkout", "OldAlert")
        pattern.last_nfa_at = datetime.now(timezone.utc) - timedelta(days=60)
        db_session.commit()

        count = repo.expire_stale_patterns(reset_days=30)
        db_session.commit()

        assert count >= 1
        fresh = repo.get_by_fingerprint("fp-stale")
        assert fresh is not None
        assert fresh.nfa_count == 0


# ---------------------------------------------------------------------------
# FeedbackItemRepository
# ---------------------------------------------------------------------------


class TestFeedbackItemRepository:
    def test_create_and_list_for_incident(self, db_session: Session) -> None:
        db_session.commit()
        repo = FeedbackItemRepository(db_session)
        repo.create(
            incident_id="inc_1",
            agent_run_id=None,
            feedback_type="root_cause_correction",
            original_value={"root_cause_summary": "old"},
            corrected_value={"root_cause_summary": "new"},
            delta={"original": "old", "corrected": "new"},
            submitted_by="sre-oncall",
        )
        db_session.commit()

        items = repo.list_for_incident("inc_1")
        assert len(items) == 1
        assert items[0].feedback_type == "root_cause_correction"

    def test_list_for_eval_excludes_nfa(self, db_session: Session) -> None:
        db_session.commit()
        repo = FeedbackItemRepository(db_session)
        repo.create(
            incident_id="inc_1",
            agent_run_id=None,
            feedback_type="root_cause_correction",
            original_value={},
            corrected_value={},
            delta={},
        )
        repo.create(
            incident_id="inc_2",
            agent_run_id=None,
            feedback_type="nfa_mark",
            original_value={},
            corrected_value={},
            delta={},
        )
        db_session.commit()

        items = repo.list_for_eval()
        assert len(items) == 1
        assert items[0].feedback_type == "root_cause_correction"

    def test_list_for_eval_with_type_filter(self, db_session: Session) -> None:
        db_session.commit()
        repo = FeedbackItemRepository(db_session)
        repo.create(
            incident_id="inc_1",
            agent_run_id=None,
            feedback_type="action_addition",
            original_value={},
            corrected_value={"type": "restart"},
            delta={},
        )
        db_session.commit()

        items = repo.list_for_eval(feedback_type="action_addition")
        assert len(items) == 1
        assert items[0].feedback_type == "action_addition"


# ---------------------------------------------------------------------------
# IncidentCorrelationRepository
# ---------------------------------------------------------------------------


class TestIncidentCorrelationRepository:
    def test_create_and_get_for_incident(self, db_session: Session) -> None:
        db_session.commit()
        repo = IncidentCorrelationRepository(db_session)
        repo.create("inc_a", "inc_b", "same_fingerprint")
        db_session.commit()

        items = repo.get_for_incident("inc_a")
        assert len(items) == 1
        assert items[0].correlation_type == "same_fingerprint"

        items_b = repo.get_for_incident("inc_b")
        assert len(items_b) == 1

    def test_create_is_idempotent(self, db_session: Session) -> None:
        db_session.commit()
        repo = IncidentCorrelationRepository(db_session)
        first = repo.create("inc_a", "inc_b", "same_fingerprint")
        second = repo.create("inc_a", "inc_b", "same_fingerprint")
        db_session.commit()

        assert first.correlation_id == second.correlation_id

    def test_create_is_idempotent_swapped_order(self, db_session: Session) -> None:
        db_session.commit()
        repo = IncidentCorrelationRepository(db_session)
        first = repo.create("inc_a", "inc_b", "same_fingerprint")
        second = repo.create("inc_b", "inc_a", "same_fingerprint")
        db_session.commit()

        assert first.correlation_id == second.correlation_id

    def test_find_by_fingerprint(self, db_session: Session) -> None:
        db_session.commit()
        _create_incident(db_session, "inc_a", fingerprint="fp-x", alert_name="AlertX", severity="P1", status="resolved")
        _create_incident(db_session, "inc_b", fingerprint="fp-x", alert_name="AlertX", severity="P2", status="resolved")
        _create_incident(db_session, "inc_c", fingerprint="fp-y", alert_name="AlertY", severity="P3")
        db_session.commit()

        repo = IncidentCorrelationRepository(db_session)
        results = repo.find_by_fingerprint("fp-x", exclude_incident_id="inc_a")
        assert len(results) == 1
        assert results[0].incident_id == "inc_b"

    def test_find_similar_by_service(self, db_session: Session) -> None:
        db_session.commit()
        _create_incident(db_session, "inc_a", fingerprint="fp-a", service="checkout")
        _create_incident(db_session, "inc_b", fingerprint="fp-b", service="checkout")
        _create_incident(db_session, "inc_c", fingerprint="fp-c", service="payment")
        db_session.commit()

        repo = IncidentCorrelationRepository(db_session)
        results = repo.find_similar_by_service("checkout", exclude_incident_id="inc_a")
        assert len(results) == 1
        assert results[0].incident_id == "inc_b"


# ---------------------------------------------------------------------------
# FeedbackService
# ---------------------------------------------------------------------------


class TestFeedbackService:
    def test_mark_nfa(self, db_session: Session) -> None:
        db_session.commit()
        incident = _create_incident(db_session, "inc_test")
        db_session.commit()

        service = FeedbackService(db_session, _settings())
        result = service.mark_nfa("inc_test", NfaMarkRequest(reason="Noise"))
        db_session.commit()

        assert result.nfa_count == 1
        assert result.status == "active"

    def test_mark_nfa_suppresses_and_downgrades_severity(self, db_session: Session) -> None:
        db_session.commit()
        incident = _create_incident(db_session, "inc_test")
        db_session.commit()

        service = FeedbackService(db_session, _settings())
        service.mark_nfa("inc_test", NfaMarkRequest(reason="Noise"))
        service.mark_nfa("inc_test", NfaMarkRequest(reason="Noise"))
        result = service.mark_nfa("inc_test", NfaMarkRequest(reason="Noise"))
        db_session.commit()

        assert result.status == "suppressed"
        assert incident.severity == "P4"

    def test_correct_root_cause(self, db_session: Session) -> None:
        db_session.commit()
        _create_incident(db_session, "inc_test", root_cause="CPU saturation")
        db_session.commit()

        service = FeedbackService(db_session, _settings())
        result = service.correct_root_cause(
            "inc_test",
            RootCauseCorrectionRequest(
                corrected_summary="Memory leak in payment pod", reason="Actually OOM"
            ),
        )
        db_session.commit()

        assert result.feedback_type == "root_cause_correction"
        assert result.delta is not None
        assert result.delta["corrected"] == "Memory leak in payment pod"

        incident_repo = IncidentRepository(db_session)
        incident = incident_repo.get_by_public_id("inc_test")
        assert incident is not None
        assert incident.root_cause_summary == "Memory leak in payment pod"

    def test_correct_action_add(self, db_session: Session) -> None:
        db_session.commit()
        _create_incident(db_session, "inc_test")
        db_session.commit()

        service = FeedbackService(db_session, _settings())
        result = service.correct_action(
            "inc_test",
            None,
            ActionCorrectionRequest(
                action_type="add",
                action={"type": "restart_pod", "target": "checkout-abc"},
                reason="Forgot restart",
            ),
        )
        db_session.commit()

        assert result.feedback_type == "action_add"
        assert result.corrected_value == {"type": "restart_pod", "target": "checkout-abc"}

    def test_correct_action_remove(self, db_session: Session) -> None:
        db_session.commit()
        _create_incident(db_session, "inc_test")
        db_session.commit()

        service = FeedbackService(db_session, _settings())
        result = service.correct_action(
            "inc_test",
            "act_1",
            ActionCorrectionRequest(
                action_type="remove",
                action_id="act_1",
                reason="Unsafe action",
            ),
        )
        db_session.commit()

        assert result.feedback_type == "action_remove"
        assert result.original_value == {"action_id": "act_1"}

    def test_correct_action_invalid_type(self, db_session: Session) -> None:
        db_session.commit()
        _create_incident(db_session, "inc_test")
        db_session.commit()

        service = FeedbackService(db_session, _settings())
        with pytest.raises(Exception):
            service.correct_action(
                "inc_test",
                None,
                ActionCorrectionRequest(action_type="update", reason="Bad"),
            )

    def test_get_correlated_incidents(self, db_session: Session) -> None:
        db_session.commit()
        _create_incident(db_session, "inc_test", fingerprint="fp-x", service="checkout")
        _create_incident(db_session, "inc_related", fingerprint="fp-x", service="checkout",
                         alert_name="RelatedAlert", root_cause="Same fingerprint match",
                         status="resolved")
        _create_incident(db_session, "inc_samesvc", fingerprint="fp-y", service="checkout",
                         alert_name="SvcAlert", root_cause="Same service match")
        db_session.commit()

        service = FeedbackService(db_session, _settings())
        results = service.get_correlated_incidents("inc_test")

        assert len(results) >= 2
        fingerprints = {r.incident_id for r in results}
        assert "inc_related" in fingerprints
        assert "inc_samesvc" in fingerprints

    def test_list_feedback(self, db_session: Session) -> None:
        db_session.commit()
        _create_incident(db_session, "inc_test")
        db_session.commit()

        service = FeedbackService(db_session, _settings())
        service.correct_root_cause(
            "inc_test",
            RootCauseCorrectionRequest(corrected_summary="Fixed"),
        )
        db_session.commit()

        items = service.list_feedback("inc_test")
        assert len(items) == 1
        assert items[0].feedback_type == "root_cause_correction"
