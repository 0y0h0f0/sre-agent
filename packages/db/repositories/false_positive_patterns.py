"""Repository for false_positive_patterns table — NFA learning."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from packages.common.ids import new_id
from packages.common.time import utc_now
from packages.db.models import FalsePositivePattern


class FalsePositivePatternRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def get_by_fingerprint(
        self, fingerprint: str, *, for_update: bool = False
    ) -> FalsePositivePattern | None:
        stmt = select(FalsePositivePattern).where(
            FalsePositivePattern.fingerprint == fingerprint
        )
        if for_update:
            stmt = stmt.with_for_update()
        return self.db.scalar(stmt)

    def get_by_pattern_id(self, pattern_id: str) -> FalsePositivePattern | None:
        stmt = select(FalsePositivePattern).where(
            FalsePositivePattern.pattern_id == pattern_id
        )
        return self.db.scalar(stmt)

    def increment_nfa(
        self,
        fingerprint: str,
        service: str,
        alert_name: str,
        *,
        threshold: int = 3,
    ) -> FalsePositivePattern:
        """Record an NFA mark. Auto-suppresses when count reaches threshold.

        Locks the row with ``SELECT ... FOR UPDATE`` to prevent lost increments
        under concurrent NFA marks.
        """
        existing = self.get_by_fingerprint(fingerprint, for_update=True)
        now = utc_now()

        if existing is not None:
            existing.nfa_count += 1
            existing.last_nfa_at = now
            if existing.nfa_count >= threshold and existing.status == "active":
                existing.status = "suppressed"
                existing.suppressed_at = now
                existing.suppressed_by = "auto"
            self.db.flush()
            return existing

        pattern = FalsePositivePattern(
            pattern_id=new_id("nfp_"),
            fingerprint=fingerprint,
            service=service,
            alert_name=alert_name,
            nfa_count=1,
            status="active",
            first_nfa_at=now,
            last_nfa_at=now,
        )
        self.db.add(pattern)
        self.db.flush()
        return pattern

    def get_suppressed_patterns(self) -> list[FalsePositivePattern]:
        stmt = select(FalsePositivePattern).where(
            FalsePositivePattern.status == "suppressed"
        )
        return list(self.db.scalars(stmt).all())

    def get_active_patterns(self) -> list[FalsePositivePattern]:
        stmt = select(FalsePositivePattern).where(
            FalsePositivePattern.status == "active"
        )
        return list(self.db.scalars(stmt).all())

    def should_suppress(self, fingerprint: str, *, threshold: int = 3) -> bool:
        """Check if an alert should be suppressed before creating an incident.

        Uses ``SELECT ... FOR UPDATE`` to prevent a concurrent
        ``increment_nfa()`` from advancing the count while this check is
        in flight.
        """
        existing = self.get_by_fingerprint(fingerprint, for_update=True)
        if existing is None:
            return False
        if existing.status == "suppressed":
            return True
        if existing.status == "active" and existing.nfa_count >= threshold:
            existing.status = "suppressed"
            existing.suppressed_at = utc_now()
            existing.suppressed_by = "auto"
            return True
        return False

    def restore_pattern(
        self, pattern_id: str, restored_by: str = "sre"
    ) -> FalsePositivePattern | None:
        """Manually restore a suppressed pattern to active."""
        pattern = self.get_by_pattern_id(pattern_id)
        if pattern is None or pattern.status != "suppressed":
            return None
        pattern.status = "active"
        pattern.nfa_count = 0
        pattern.restored_by = restored_by
        pattern.restored_at = utc_now()
        pattern.suppressed_at = None
        pattern.suppressed_by = None
        return pattern

    def expire_stale_patterns(self, *, reset_days: int = 30) -> int:
        """Reset NFA count for patterns with no NFA mark in `reset_days` days."""
        cutoff = utc_now() - timedelta(days=reset_days)
        stmt = (
            update(FalsePositivePattern)
            .where(
                FalsePositivePattern.status == "active",
                FalsePositivePattern.last_nfa_at < cutoff,
                FalsePositivePattern.nfa_count > 0,
            )
            .values(nfa_count=0)
        )
        result = self.db.execute(stmt)
        return result.rowcount  # type: ignore[attr-defined,no-any-return]
