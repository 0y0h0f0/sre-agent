"""PollCursor repository — manages Alertmanager poll dedup and cursor state.

M4 PR 4.5: Tracks seen fingerprints per filter hash, maintains missing_rounds
for resolved inference, and ensures fingerprint -> incident_id mapping is
globally unique across filter hashes.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from packages.common.time import utc_now
from packages.db.models import AlertPollCursor


class PollCursorRepository:
    """Repository for AlertPollCursor — poll dedup and cursor state."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def already_seen_active(
        self, fingerprint: str, filter_hash: str
    ) -> bool:
        """Check if fingerprint was already seen in this poll cycle.

        IMPORTANT: Always updates last_seen_at and resets missing_rounds,
        even when returning True (intentional side effect for cursor tracking).
        """
        stmt = select(AlertPollCursor).where(
            AlertPollCursor.fingerprint == fingerprint,
            AlertPollCursor.filter_hash == filter_hash,
        )
        result = self.db.execute(stmt)
        cursor = result.scalar_one_or_none()

        now = utc_now()
        if cursor is not None:
            cursor.last_seen_at = now
            cursor.missing_rounds = 0
            self.db.commit()
            return True
        return False

    def mark_seen(
        self,
        fingerprint: str,
        incident_id: str,
        filter_hash: str,
    ) -> None:
        """Create or update cursor entry for a seen fingerprint."""
        now = utc_now()
        stmt = select(AlertPollCursor).where(
            AlertPollCursor.fingerprint == fingerprint,
            AlertPollCursor.filter_hash == filter_hash,
        )
        result = self.db.execute(stmt)
        cursor = result.scalar_one_or_none()

        if cursor is not None:
            cursor.incident_id = incident_id
            cursor.last_seen_at = now
            cursor.missing_rounds = 0
        else:
            cursor = AlertPollCursor(
                filter_hash=filter_hash,
                fingerprint=fingerprint,
                incident_id=incident_id,
                last_seen_at=now,
                first_seen_at=now,
                missing_rounds=0,
            )
            self.db.add(cursor)
        self.db.commit()

    def mark_missing(
        self, fingerprint: str, filter_hash: str
    ) -> None:
        """Increment missing_rounds for a fingerprint not seen in this poll."""
        stmt = select(AlertPollCursor).where(
            AlertPollCursor.fingerprint == fingerprint,
            AlertPollCursor.filter_hash == filter_hash,
        )
        result = self.db.execute(stmt)
        cursor = result.scalar_one_or_none()

        if cursor is not None:
            cursor.missing_rounds += 1
            self.db.commit()
        # If cursor doesn't exist, it means this fingerprint was never
        # seen by this filter hash — nothing to mark as missing.

    def get_missing_rounds(
        self, fingerprint: str, filter_hash: str
    ) -> int:
        """Return the number of consecutive missing rounds for a fingerprint."""
        stmt = select(AlertPollCursor.missing_rounds).where(
            AlertPollCursor.fingerprint == fingerprint,
            AlertPollCursor.filter_hash == filter_hash,
        )
        result = self.db.execute(stmt)
        val = result.scalar()
        return val if val is not None else 0

    def get_filter_hashes_for_fingerprint(
        self, fingerprint: str
    ) -> list[str]:
        """Return all filter hashes that have ever seen this fingerprint."""
        stmt = select(AlertPollCursor.filter_hash).where(
            AlertPollCursor.fingerprint == fingerprint,
        )
        result = self.db.execute(stmt)
        return list(result.scalars().all())

    def get_first_seen_at(
        self, fingerprint: str
    ) -> float | None:
        """Return the first_seen_at timestamp for a fingerprint."""
        stmt = (
            select(AlertPollCursor.first_seen_at)
            .where(AlertPollCursor.fingerprint == fingerprint)
            .order_by(AlertPollCursor.first_seen_at.asc())
            .limit(1)
        )
        result = self.db.execute(stmt)
        val = result.scalar()
        if val is not None:
            return val.timestamp()
        return None

    def get_active_fingerprints(
        self, filter_hash: str
    ) -> list[str]:
        """Return fingerprints actively seen by a filter hash (missing_rounds == 0)."""
        stmt = select(AlertPollCursor.fingerprint).where(
            AlertPollCursor.filter_hash == filter_hash,
            AlertPollCursor.missing_rounds == 0,
        )
        result = self.db.execute(stmt)
        return list(result.scalars().all())
