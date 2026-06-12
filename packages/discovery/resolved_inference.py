"""Resolved inference for Alertmanager poll alerts.

M4 PR 4.6: Conservative resolved inference. Never infers resolved from
truncated results. Multi-filter-hash: incident only resolved when ALL
active filter hashes consecutively missing enough rounds.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Protocol


class PollCursorProtocol(Protocol):
    """Protocol for poll cursor repository (avoids circular imports)."""

    def get_missing_rounds(self, fingerprint: str, filter_hash: str) -> int: ...
    def get_filter_hashes_for_fingerprint(self, fingerprint: str) -> list[str]: ...
    def get_first_seen_at(self, fingerprint: str) -> float | None: ...


@dataclass
class ResolvedDecision:
    """Result of resolved inference for a single fingerprint."""

    fingerprint: str
    is_resolved: bool
    reason: str
    evidence: dict[str, Any] = field(default_factory=dict)


def infer_resolved_from_missing_fingerprints(
    fingerprint: str,
    all_active_filter_hashes: list[str],
    cursor_repo: PollCursorProtocol,
    results_truncated: bool = False,
    grace_rounds: int = 3,
    resolved_rounds: int = 3,
    poll_interval_seconds: int = 30,
) -> ResolvedDecision:
    """Determine if an incident should be inferred as resolved.

    Rules (in order):
    1. Truncation blocks resolved inference entirely.
    2. Fingerprint within grace period is not eligible.
    3. If ANY active filter hash still sees the fingerprint -> not resolved.
    4. ALL active filter hashes must have fingerprint missing >= resolved_rounds.
    5. Only filter hashes that have ever seen this fingerprint participate.
    """
    if results_truncated:
        return ResolvedDecision(
            fingerprint=fingerprint,
            is_resolved=False,
            reason="truncated_results",
            evidence={"truncated": True},
        )

    # Grace period check.
    first_seen = cursor_repo.get_first_seen_at(fingerprint)
    if first_seen is not None:
        now = time.time()
        grace_seconds = grace_rounds * poll_interval_seconds
        if (now - first_seen) < grace_seconds:
            return ResolvedDecision(
                fingerprint=fingerprint,
                is_resolved=False,
                reason="grace_period",
                evidence={
                    "first_seen_at": first_seen,
                    "grace_seconds": grace_seconds,
                    "elapsed_seconds": now - first_seen,
                },
            )

    known_hashes = cursor_repo.get_filter_hashes_for_fingerprint(fingerprint)
    if not known_hashes:
        return ResolvedDecision(
            fingerprint=fingerprint,
            is_resolved=False,
            reason="never_seen",
            evidence={},
        )

    all_missing = True
    missing_details: dict[str, int] = {}
    for fh in known_hashes:
        missing = cursor_repo.get_missing_rounds(fingerprint, fh)
        missing_details[fh] = missing
        if missing < resolved_rounds:
            all_missing = False

    if all_missing:
        return ResolvedDecision(
            fingerprint=fingerprint,
            is_resolved=True,
            reason="all_filter_hashes_missing",
            evidence={
                "filter_hashes": known_hashes,
                "missing_rounds": missing_details,
                "resolved_rounds": resolved_rounds,
            },
        )

    return ResolvedDecision(
        fingerprint=fingerprint,
        is_resolved=False,
        reason="insufficient_missing_rounds",
        evidence={
            "missing_rounds": missing_details,
            "resolved_rounds": resolved_rounds,
        },
    )
