"""DiscoveryStore — persist DiscoveryRun + DiscoveryProposal to database.

M3 PR 3.4: Wraps repository operations for persisting discovery results.
Designed to be called from Celery tasks (scheduled/manual rerun) with
Redis lock safety at the task level.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from packages.common.time import utc_now
from packages.db.repositories.discovery_proposals import DiscoveryProposalRepository
from packages.db.repositories.discovery_runs import DiscoveryRunRepository
from packages.discovery.models import DiscoveryResult


class DiscoveryStore:
    """Persists DiscoveryResult and manages DiscoveryRun/DiscoveryProposal lifecycle.

    Usage::

        store = DiscoveryStore(db_session)
        run = store.create_run(source="scheduled", trigger_type="automatic")
        # ... run discovery ...
        store.finish_run(run, result, status="succeeded")
        proposal = store.create_proposal(run.discovery_run_id, config_diff, confidence)
    """

    def __init__(self, db: Session) -> None:
        self._db = db
        self._run_repo = DiscoveryRunRepository(db)
        self._proposal_repo = DiscoveryProposalRepository(db)

    # ------------------------------------------------------------------
    # DiscoveryRun
    # ------------------------------------------------------------------

    def create_run(
        self,
        *,
        source: str,
        trigger_type: str = "automatic",
        triggered_by: str | None = None,
    ) -> Any:  # DiscoveryRun
        """Create a new DiscoveryRun in 'running' state."""
        return self._run_repo.create(
            source=source,
            trigger_type=trigger_type,
            triggered_by=triggered_by,
            summary={},
        )

    def get_run(self, discovery_run_id: str) -> Any | None:
        """Get a DiscoveryRun by ID."""
        return self._run_repo.get_by_id(discovery_run_id)

    def finish_run(
        self,
        run: Any,  # DiscoveryRun
        result: DiscoveryResult,
        *,
        status: str | None = None,
        error_message: str | None = None,
    ) -> None:
        """Mark a DiscoveryRun as finished with summary from result."""
        effective_status = status or result.status
        self._run_repo.mark_finished(
            run,
            status=effective_status,
            error_message=error_message,
            summary={
                "total_services_discovered": result.total_services_discovered,
                "total_metrics_scanned": result.total_metrics_scanned,
                "duration_seconds": result.duration_seconds,
                "warnings": result.warnings,
                "degraded_signals": result.degraded_signals,
                "backend_count": len(result.backend_endpoints),
            },
        )
        self._db.flush()

    def list_recent_runs(self, limit: int = 20) -> list[Any]:
        """List recent discovery runs."""
        return list(self._run_repo.list_recent(limit))

    # ------------------------------------------------------------------
    # DiscoveryProposal
    # ------------------------------------------------------------------

    def create_proposal(
        self,
        *,
        discovery_run_id: str,
        config_diff: dict[str, Any] | None = None,
        confidence: float | None = None,
        status: str = "pending_review",
    ) -> Any:  # DiscoveryProposal
        """Create a DiscoveryProposal for a finished run."""
        return self._proposal_repo.create(
            discovery_run_id=discovery_run_id,
            status=status,
            config_diff=config_diff or {},
            confidence=confidence,
        )

    def get_proposal(self, proposal_id: str) -> Any | None:
        """Get a DiscoveryProposal by ID."""
        return self._proposal_repo.get_by_id(proposal_id)

    def list_proposals_for_run(self, discovery_run_id: str) -> list[Any]:
        """List proposals for a specific run."""
        return list(self._proposal_repo.list_for_run(discovery_run_id))

    def list_pending_proposals(self, limit: int = 50) -> list[Any]:
        """List proposals awaiting review."""
        return list(self._proposal_repo.list_pending_review(limit))

    def update_proposal_status(
        self,
        proposal: Any,  # DiscoveryProposal
        status: str,
        *,
        reviewed_by: str | None = None,
        rejected_reason: str | None = None,
    ) -> None:
        """Update a proposal's status (e.g., after review)."""
        proposal.status = status
        if reviewed_by is not None:
            proposal.reviewed_by = reviewed_by
        if rejected_reason is not None:
            proposal.rejected_reason = rejected_reason
        if status == "auto_applied":
            proposal.applied_at = utc_now()
        self._db.flush()

    def supersede_proposals(self, discovery_run_id: str) -> None:
        """Mark all pending proposals for a run as superseded."""
        for proposal in self._proposal_repo.list_for_run(discovery_run_id):
            if proposal.status == "pending_review":
                proposal.status = "superseded"
        self._db.flush()
