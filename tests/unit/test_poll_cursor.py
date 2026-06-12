"""Tests for M4 PR 4.5: Poll Cursor / Dedup."""
from __future__ import annotations

from packages.db.models import AlertPollCursor


class TestAlertPollCursorModel:
    def test_model_fields(self):
        """Verify AlertPollCursor model has expected fields."""
        assert hasattr(AlertPollCursor, "filter_hash")
        assert hasattr(AlertPollCursor, "fingerprint")
        assert hasattr(AlertPollCursor, "incident_id")
        assert hasattr(AlertPollCursor, "last_seen_at")
        assert hasattr(AlertPollCursor, "first_seen_at")
        assert hasattr(AlertPollCursor, "missing_rounds")

    def test_table_name(self):
        assert AlertPollCursor.__tablename__ == "alert_poll_cursors"

    def test_unique_constraint(self):
        """The model has a unique constraint on filter_hash + fingerprint."""
        args = getattr(AlertPollCursor, "__table_args__", None)
        assert args is not None

    def test_default_missing_rounds_zero(self):
        cursor = AlertPollCursor(
            filter_hash="hash1",
            fingerprint="fp1",
            missing_rounds=0,
        )
        assert cursor.missing_rounds == 0
