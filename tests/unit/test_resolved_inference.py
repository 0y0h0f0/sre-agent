"""Tests for M4 PR 4.6: Resolved Inference."""
from __future__ import annotations

from packages.discovery.resolved_inference import (
    infer_resolved_from_missing_fingerprints,
)


class _MockCursorRepo:
    """Mock PollCursorRepository for testing."""
    def __init__(self, missing_rounds=0, filter_hashes=None, first_seen_at=None):
        self._missing = missing_rounds
        self._hashes = filter_hashes  # None means default, [] means empty
        self._first_seen = first_seen_at

    def get_missing_rounds(self, fp, fh):
        return self._missing

    def get_filter_hashes_for_fingerprint(self, fp):
        if self._hashes is None:
            return ["hash1"]
        return list(self._hashes)

    def get_first_seen_at(self, fp):
        return self._first_seen


class TestResolvedInference:
    def test_missing_enough_rounds_resolved(self):
        repo = _MockCursorRepo(missing_rounds=3)
        result = infer_resolved_from_missing_fingerprints(
            "fp1", ["hash1"], repo, resolved_rounds=3,
        )
        assert result.is_resolved

    def test_grace_period_blocks_resolved(self):
        import time
        repo = _MockCursorRepo(
            missing_rounds=3, first_seen_at=time.time(),
        )
        result = infer_resolved_from_missing_fingerprints(
            "fp1", ["hash1"], repo, grace_rounds=3, resolved_rounds=3,
            poll_interval_seconds=30,
        )
        assert not result.is_resolved
        assert result.reason == "grace_period"

    def test_truncation_blocks_resolved(self):
        repo = _MockCursorRepo(missing_rounds=10)
        result = infer_resolved_from_missing_fingerprints(
            "fp1", ["hash1"], repo, results_truncated=True, resolved_rounds=3,
        )
        assert not result.is_resolved
        assert result.reason == "truncated_results"

    def test_first_missing_not_resolved(self):
        repo = _MockCursorRepo(missing_rounds=1)
        result = infer_resolved_from_missing_fingerprints(
            "fp1", ["hash1"], repo, resolved_rounds=3,
        )
        assert not result.is_resolved

    def test_single_filter_hash_missing_does_not_resolve(self):
        # hash1 has enough missing rounds, but hash2 has 0 (still seeing the fp).
        class _MultiMockRepo:
            def get_missing_rounds(self, fp, fh):
                return 3 if fh == "hash1" else 0
            def get_filter_hashes_for_fingerprint(self, fp):
                return ["hash1", "hash2"]
            def get_first_seen_at(self, fp):
                return 0  # very old, past grace period
        repo = _MultiMockRepo()
        result = infer_resolved_from_missing_fingerprints(
            "fp1", ["hash1", "hash2"], repo, resolved_rounds=3,
        )
        assert not result.is_resolved

    def test_all_active_hashes_missing_resolves(self):
        repo = _MockCursorRepo(
            missing_rounds=3, filter_hashes=["hash1", "hash2"],
            first_seen_at=0,  # very old
        )
        result = infer_resolved_from_missing_fingerprints(
            "fp1", ["hash1", "hash2"], repo, resolved_rounds=3,
        )
        assert result.is_resolved

    def test_never_seen_returns_not_resolved(self):
        repo = _MockCursorRepo(filter_hashes=[], first_seen_at=None)
        result = infer_resolved_from_missing_fingerprints(
            "fp1", ["hash1"], repo, resolved_rounds=3,
        )
        assert not result.is_resolved
        assert result.reason == "never_seen"
