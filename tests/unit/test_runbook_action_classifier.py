"""PR 9.2 — Runbook action classifier tests."""

from __future__ import annotations

import pytest

from packages.rag.runbook_action_classifier import (
    ActionClassification,
    ActionStep,
    RunbookActionClassifier,
)


class TestRunbookActionClassifier:
    def setup_method(self):
        self.classifier = RunbookActionClassifier()

    def test_classifies_read_only_actions(self):
        steps = self.classifier.classify(
            "## Actions\n"
            "1. Check the Prometheus dashboard for error rate\n"
            "2. Query logs for the last 15 minutes\n"
            "3. Review recent deployment history\n"
        )
        for step in steps:
            assert step.classification == ActionClassification.READ_ONLY, (
                f"Expected read_only for '{step.text}', got {step.classification}"
            )

    def test_classifies_diagnostic_only_actions(self):
        steps = self.classifier.classify(
            "## Actions\n"
            "1. Run a CPU profile on the affected pod\n"
            "2. Execute a thread dump on the JVM\n"
            "3. Check database connection pool metrics\n"
        )
        for step in steps:
            assert step.classification in (
                ActionClassification.READ_ONLY,
                ActionClassification.DIAGNOSTIC_ONLY,
            )

    def test_classifies_approval_required_actions(self):
        steps = self.classifier.classify(
            "## Actions\n"
            "1. Restart the checkout service deployment\n"
            "2. Scale the backend from 3 to 5 replicas\n"
            "3. Rollback the latest release\n"
        )
        classes = {s.classification for s in steps}
        assert ActionClassification.APPROVAL_REQUIRED in classes

    def test_classifies_forbidden_actions(self):
        steps = self.classifier.classify(
            "## Actions\n"
            "1. Delete the users table\n"
            "2. Drop the cache database\n"
            "3. Truncate the sessions table\n"
            "4. Flush all Redis caches\n"
        )
        for step in steps:
            assert step.classification == ActionClassification.FORBIDDEN, (
                f"Expected forbidden for '{step.text}', got {step.classification}"
            )

    def test_classifies_unknown_actions(self):
        steps = self.classifier.classify(
            "## Actions\n"
            "1. Do something mysterious\n"
        )
        assert steps[0].classification == ActionClassification.UNKNOWN

    def test_forbidden_keywords_caught(self):
        """Each forbidden keyword individually triggers forbidden."""
        for keyword in ("delete", "drop", "truncate", "flush", "modify_database"):
            steps = self.classifier.classify(
                f"## Actions\n1. {keyword} something important\n"
            )
            assert steps[0].classification == ActionClassification.FORBIDDEN, (
                f"'{keyword}' should be forbidden"
            )

    def test_no_actions_returns_empty_list(self):
        steps = self.classifier.classify("## Detection\nCheck the dashboard for errors.")
        assert steps == []

    def test_empty_content_returns_empty_list(self):
        assert self.classifier.classify("") == []

    def test_output_format_is_serializable(self):
        steps = self.classifier.classify(
            "## Actions\n1. Check metrics\n2. Restart service\n3. Delete data\n"
        )
        summary = self.classifier.classification_summary(steps)
        assert isinstance(summary, dict)
        assert "steps" in summary
        assert "counts" in summary
        counts = summary["counts"]
        assert isinstance(counts["read_only"], int)
        assert isinstance(counts["forbidden"], int)
        assert isinstance(counts["approval_required"], int)

    def test_forbidden_present_makes_requires_review_true(self):
        steps = self.classifier.classify(
            "## Actions\n1. Delete something\n"
        )
        summary = self.classifier.classification_summary(steps)
        assert summary["counts"]["forbidden"] >= 1

    def test_approval_required_triggers_on_keywords(self):
        for keyword in ("restart", "scale", "rollback", "revert"):
            steps = self.classifier.classify(
                f"## Actions\n1. {keyword} the deployment\n"
            )
            if steps:
                assert steps[0].classification in (
                    ActionClassification.APPROVAL_REQUIRED,
                    ActionClassification.FORBIDDEN,
                ), f"'{keyword}' should require approval or be forbidden"


class TestActionClassificationEnum:
    def test_all_values_present(self):
        expected = {"read_only", "diagnostic_only", "approval_required", "forbidden", "unknown"}
        actual = {e.value for e in ActionClassification}
        assert expected == actual


class TestActionStep:
    def test_action_step_creation(self):
        step = ActionStep(
            index=1,
            text="Restart the checkout deployment",
            classification=ActionClassification.APPROVAL_REQUIRED,
            matched_keywords=["restart"],
        )
        assert step.index == 1
        assert step.classification == ActionClassification.APPROVAL_REQUIRED
        assert step.matched_keywords == ["restart"]
