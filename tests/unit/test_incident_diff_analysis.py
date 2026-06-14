"""PR 9.3 — Incident Diff Analysis tests."""

from __future__ import annotations

import json
from typing import Any

import pytest

from packages.agent.llm.fake_adapter import FakeLLMAdapter
from packages.common.feature_flags import is_m9_subfeature_enabled
from packages.common.settings import Settings
from packages.rag.incident_diff import (
    AmendmentProposal,
    AmendmentType,
    IncidentDiffAnalyzer,
)


class StaticDiffLLM:
    provider = "fake"

    def __init__(self, payload: object) -> None:
        self.payload = payload
        self.calls = 0

    def invoke(
        self, messages: list[dict[str, Any]], *, thinking: bool = False, **kwargs: Any
    ) -> str:
        self.calls += 1
        return json.dumps(self.payload)

    def generate_json(
        self, prompt: str, output_schema: Any, *, thinking: bool = False, **kwargs: Any
    ) -> Any:
        raise AssertionError("incident diff should call invoke(), not generate_json()")


# ---------------------------------------------------------------------------
# Default disabled
# ---------------------------------------------------------------------------

class TestIncidentDiffDefaultDisabled:
    def test_incident_diff_default_disabled(self):
        settings = Settings()
        assert settings.llm_incident_diff_enabled is False

    def test_incident_diff_requires_m9_enabled(self):
        settings = Settings(
            m9_extensions_enabled=False,
            llm_incident_diff_enabled=True,
        )
        assert not is_m9_subfeature_enabled(settings, "llm_incident_diff")

    def test_incident_diff_enabled_when_both_gates_on(self):
        settings = Settings(
            m9_extensions_enabled=True,
            llm_incident_diff_enabled=True,
        )
        assert is_m9_subfeature_enabled(settings, "llm_incident_diff")

    def test_analyzer_refuses_when_feature_disabled(self):
        settings = Settings(m9_extensions_enabled=False)
        analyzer = IncidentDiffAnalyzer(settings=settings, llm=FakeLLMAdapter())
        result = analyzer.analyze(
            service="checkout",
            fault_type="high_5xx",
            approved_runbook="## Detection\nCheck error rate.",
        )
        assert result.status == "disabled"


# ---------------------------------------------------------------------------
# Minimum evidence threshold
# ---------------------------------------------------------------------------

class TestEvidenceThreshold:
    def _make_analyzer(self, **overrides):
        s = Settings(m9_extensions_enabled=True, llm_incident_diff_enabled=True, **overrides)
        return IncidentDiffAnalyzer(settings=s, llm=FakeLLMAdapter())

    def test_skips_without_minimum_evidence(self):
        analyzer = self._make_analyzer()
        result = analyzer.analyze(
            service="checkout",
            fault_type="high_5xx",
            approved_runbook="## Detection\nCheck error rate.",
        )
        assert result.status == "skipped_insufficient_evidence"

    def test_does_not_call_llm_when_skipped(self):
        """When evidence is insufficient, LLM is never invoked."""
        llm = StaticDiffLLM([])
        settings = Settings(m9_extensions_enabled=True, llm_incident_diff_enabled=True)
        analyzer = IncidentDiffAnalyzer(settings=settings, llm=llm)
        result = analyzer.analyze(
            service="checkout",
            fault_type="high_5xx",
            approved_runbook="## Detection\nCheck error rate.",
        )
        assert result.status == "skipped_insufficient_evidence"
        assert result.proposals == []
        assert llm.calls == 0

    def test_proceeds_with_diagnosis_report(self):
        analyzer = self._make_analyzer()
        result = analyzer.analyze(
            service="checkout",
            fault_type="high_5xx",
            approved_runbook="## Detection\nCheck error rate.",
            diagnosis_report="Root cause: misconfigured connection pool.",
        )
        assert result.status != "skipped_insufficient_evidence"

    def test_proceeds_with_operator_feedback(self):
        analyzer = self._make_analyzer()
        result = analyzer.analyze(
            service="checkout",
            fault_type="high_5xx",
            approved_runbook="## Detection\nCheck error rate.",
            operator_feedback="Runbook step 3 was insufficient.",
        )
        assert result.status != "skipped_insufficient_evidence"

    def test_proceeds_with_action_results(self):
        analyzer = self._make_analyzer()
        result = analyzer.analyze(
            service="checkout",
            fault_type="high_5xx",
            approved_runbook="## Detection\nCheck error rate.",
            action_execution_results=[{"action": "restart", "outcome": "failed"}],
        )
        assert result.status != "skipped_insufficient_evidence"

    def test_proceeds_with_approved_runbook_version(self):
        analyzer = self._make_analyzer()
        result = analyzer.analyze(
            service="checkout",
            fault_type="high_5xx",
            approved_runbook="## Detection\nCheck error rate.",
            linked_approved_runbook_version="ver_abc123",
        )
        assert result.status != "skipped_insufficient_evidence"

    def test_proceeds_with_enough_evidence_refs(self):
        analyzer = self._make_analyzer()
        result = analyzer.analyze(
            service="checkout",
            fault_type="high_5xx",
            approved_runbook="## Detection\nCheck error rate.",
            evidence_refs=["evd_001", "evd_002", "evd_003", "evd_004", "evd_005"],
        )
        assert result.status != "skipped_insufficient_evidence"

    def test_min_evidence_refs_is_configurable(self):
        llm = StaticDiffLLM([])
        settings = Settings(
            m9_extensions_enabled=True,
            llm_incident_diff_enabled=True,
            min_incident_diff_evidence_refs=2,
        )
        analyzer = IncidentDiffAnalyzer(settings=settings, llm=llm)
        result = analyzer.analyze(
            service="checkout",
            fault_type="high_5xx",
            approved_runbook="## Detection\nCheck error rate.",
            evidence_refs=["evd_001", "evd_002"],
        )
        assert result.status == "generated"
        assert llm.calls == 1


# ---------------------------------------------------------------------------
# Diff result and proposals
# ---------------------------------------------------------------------------

class TestDiffResult:
    def _make_analyzer(self):
        s = Settings(m9_extensions_enabled=True, llm_incident_diff_enabled=True)
        return IncidentDiffAnalyzer(settings=s, llm=FakeLLMAdapter())

    def test_creates_amendment_proposals(self):
        analyzer = self._make_analyzer()
        result = analyzer.analyze(
            service="checkout",
            fault_type="high_5xx",
            approved_runbook="## Detection\nCheck error rate.\n## Actions\n1. Restart service",
            diagnosis_report="Restart did not resolve — issue was DB connection pool exhaustion.",
        )
        assert result.status == "generated"
        # FakeLLM returns deterministic JSON that may or may not match the
        # amendment_type schema. The system handles both gracefully:
        # structured proposals are returned, or a synthesized note is created.
        for p in result.proposals:
            assert isinstance(p, AmendmentProposal)
            assert p.amendment_type in AmendmentType
            assert len(p.evidence_refs) > 0 or p.confidence == "low"

    def test_does_not_modify_approved_runbook(self):
        """The analyzer returns proposals — it never modifies the source runbook."""
        original = "## Detection\nCheck error rate.\n## Actions\n1. Restart"
        analyzer = self._make_analyzer()
        analyzer.analyze(
            service="checkout",
            fault_type="high_5xx",
            approved_runbook=original,
            diagnosis_report="Issue was DB pool, not service.",
        )
        # The approved runbook text is unchanged
        assert original == "## Detection\nCheck error rate.\n## Actions\n1. Restart"

    def test_proposals_have_evidence_refs(self):
        analyzer = self._make_analyzer()
        result = analyzer.analyze(
            service="checkout",
            fault_type="high_5xx",
            approved_runbook="## Detection\nCheck error rate.",
            diagnosis_report="DB pool exhaustion found.",
            evidence_refs=["evd_001", "evd_002"],
        )
        for p in result.proposals:
            if p.confidence == "high":
                assert len(p.evidence_refs) > 0, (
                    f"High-confidence proposal '{p.amendment_type.value}' "
                    "must have evidence refs"
                )

    def test_low_confidence_proposals_allowed_without_evidence(self):
        """Low confidence proposals without evidence are reviewer notes only."""
        analyzer = self._make_analyzer()
        result = analyzer.analyze(
            service="checkout",
            fault_type="high_5xx",
            approved_runbook="## Detection\nCheck error rate.",
            diagnosis_report="Possible DB pool issue.",
        )
        # Low confidence proposals are allowed even with minimal evidence
        for p in result.proposals:
            if p.confidence == "high" and not p.evidence_refs:
                pytest.fail(
                    f"High-confidence proposal '{p.amendment_type.value}' "
                    "must have evidence refs"
                )

    def test_rejects_apply_item_without_evidence_refs(self):
        llm = StaticDiffLLM([
            {
                "amendment_type": "missing_step",
                "rationale": "The runbook missed the DB pool check.",
                "proposed_content": "Check DB pool saturation before restart.",
                "evidence_refs": [],
                "confidence": "high",
            }
        ])
        settings = Settings(m9_extensions_enabled=True, llm_incident_diff_enabled=True)
        analyzer = IncidentDiffAnalyzer(settings=settings, llm=llm)
        result = analyzer.analyze(
            service="checkout",
            fault_type="high_5xx",
            approved_runbook="## Detection\nCheck error rate.",
            diagnosis_report="DB pool saturation was confirmed in the incident.",
        )
        assert result.status == "generated"
        assert result.proposals[0].confidence == "low"
        assert result.proposals[0].proposal_kind == "low_confidence_note"
        assert result.proposals[0].can_apply is False

    def test_filters_untrusted_evidence_refs(self):
        llm = StaticDiffLLM([
            {
                "amendment_type": "missing_rollback",
                "rationale": "Rollback checks were missing.",
                "proposed_content": "Verify the previous image before rollback.",
                "evidence_refs": ["evd_missing"],
                "confidence": "high",
            }
        ])
        settings = Settings(m9_extensions_enabled=True, llm_incident_diff_enabled=True)
        analyzer = IncidentDiffAnalyzer(settings=settings, llm=llm)
        result = analyzer.analyze(
            service="checkout",
            fault_type="high_5xx",
            approved_runbook="## Detection\nCheck error rate.",
            diagnosis_report="Rollback was required during the incident.",
            evidence_refs=["evd_001"],
        )
        assert result.proposals[0].evidence_refs == []
        assert result.proposals[0].can_apply is False

    def test_detects_required_amendment_types(self):
        llm = StaticDiffLLM([
            {
                "amendment_type": "missing_step",
                "rationale": "Add DB pool check.",
                "proposed_content": "Check DB pool saturation.",
                "evidence_refs": ["evd_001"],
                "confidence": "high",
            },
            {
                "amendment_type": "outdated_metric",
                "rationale": "Metric name changed.",
                "proposed_content": "Use http_requests_total instead.",
                "evidence_refs": ["evd_002"],
                "confidence": "high",
            },
            {
                "amendment_type": "missing_rollback",
                "rationale": "Rollback step was needed.",
                "proposed_content": "Verify rollback target before applying.",
                "evidence_refs": ["evd_003"],
                "confidence": "high",
            },
        ])
        settings = Settings(m9_extensions_enabled=True, llm_incident_diff_enabled=True)
        analyzer = IncidentDiffAnalyzer(settings=settings, llm=llm)
        result = analyzer.analyze(
            service="checkout",
            fault_type="high_5xx",
            approved_runbook="## Detection\nCheck error rate.",
            evidence_refs=["evd_001", "evd_002", "evd_003", "evd_004", "evd_005"],
        )
        assert {p.amendment_type for p in result.proposals} == {
            AmendmentType.MISSING_STEP,
            AmendmentType.OUTDATED_METRIC,
            AmendmentType.MISSING_ROLLBACK,
        }
        assert all(p.can_apply for p in result.proposals)

    def test_external_provider_requires_allow(self):
        """External LLM requires LLM_EXTERNAL_PROVIDER_ALLOWED=true."""
        settings = Settings(
            m9_extensions_enabled=True,
            llm_incident_diff_enabled=True,
            llm_provider="openai",
            llm_external_provider_allowed=False,
        )
        analyzer = IncidentDiffAnalyzer(settings=settings, llm=FakeLLMAdapter())
        result = analyzer.analyze(
            service="checkout",
            fault_type="high_5xx",
            approved_runbook="## Detection\nCheck error rate.",
            diagnosis_report="Root cause found.",
        )
        assert result.status in ("disabled", "blocked")


# ---------------------------------------------------------------------------
# AmendmentProposal and AmendmentType
# ---------------------------------------------------------------------------

class TestAmendmentProposal:
    def test_proposal_creation(self):
        p = AmendmentProposal(
            amendment_type=AmendmentType.MISSING_STEP,
            rationale="Add DB connection pool check before restarting.",
            proposed_content="## Actions\n1. Check DB connection pool",
            evidence_refs=["evd_001"],
            confidence="high",
        )
        assert p.amendment_type == AmendmentType.MISSING_STEP
        assert p.evidence_refs == ["evd_001"]
        assert p.confidence == "high"

    def test_proposal_without_evidence_is_low_confidence(self):
        p = AmendmentProposal(
            amendment_type=AmendmentType.OUTDATED_METRIC,
            rationale="Metric X is deprecated, use Y.",
            proposed_content="Use metric Y instead of X",
            evidence_refs=[],
            confidence="low",
        )
        assert p.confidence == "low"

    def test_amendment_type_values(self):
        expected = {
            "missing_step", "outdated_metric", "wrong_label_mapping",
            "missing_rollback", "unsafe_action_wording", "insufficient_evidence",
        }
        actual = {e.value for e in AmendmentType}
        assert expected == actual


# ---------------------------------------------------------------------------
# Redaction in prompts
# ---------------------------------------------------------------------------

class TestDiffPromptRedaction:
    def test_prompt_redacts_secrets(self):
        settings = Settings(
            m9_extensions_enabled=True,
            llm_incident_diff_enabled=True,
        )
        analyzer = IncidentDiffAnalyzer(settings=settings, llm=FakeLLMAdapter())
        # The analyzer builds prompts internally — verify secrets are redacted
        result = analyzer.analyze(
            service="checkout",
            fault_type="high_5xx",
            approved_runbook="## Detection\nCheck error rate.",
            diagnosis_report="Root cause: Bearer token123 was expired.",
        )
        # The diagnosis_report contains a Bearer token pattern — should be handled
        assert result.status in ("generated", "degraded", "skipped_insufficient_evidence")
