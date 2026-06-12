"""AutomationPolicy — pure-function decision engine for discovery proposals.

Determines whether a discovery proposal item can be auto-applied or requires
human review, based on AUTOMATION_LEVEL, DISCOVERY_APPLY_MODE, APP_ENV,
and the nature of the proposed change.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

AutomationLevel = Literal["off", "propose", "supervised", "autopilot"]
ApplyMode = Literal["inherit", "propose", "supervised"]
DecisionOutcome = Literal[
    "record_only", "auto_apply", "requires_review", "detected_only", "rejected"
]
ChangeType = Literal[
    "backend_url",
    "service_label",
    "metric_mapping",
    "executor_config",
    "auth_config",
    "other",
]

# Threshold confidence values for auto-apply decisions.
_SUPERVISED_THRESHOLD = 0.80
_AUTOPILOT_THRESHOLD = 0.65

# Change types that are ALWAYS dangerous and never auto-applied.
_NEVER_AUTO_APPLY: set[ChangeType] = {"executor_config"}

# Outcome priority for aggregating multiple decisions (higher = more conservative).
_OUTCOME_PRIORITY: dict[DecisionOutcome, int] = {
    "record_only": 1,
    "auto_apply": 2,
    "detected_only": 3,
    "requires_review": 4,
    "rejected": 5,
}


@dataclass
class DecisionItem:
    """A single decision about one proposal item."""

    change_type: ChangeType
    outcome: DecisionOutcome
    reason: str
    confidence: float | None = None


@dataclass
class AutomationDecision:
    """Aggregate automation decision for a discovery proposal."""

    items: list[DecisionItem] = field(default_factory=list)
    overall_outcome: DecisionOutcome = "record_only"

    @property
    def can_auto_apply(self) -> bool:
        return self.overall_outcome == "auto_apply"

    @property
    def requires_review(self) -> bool:
        return self.overall_outcome in ("requires_review", "detected_only")


def _resolve_effective_mode(
    automation_level: AutomationLevel,
    apply_mode: ApplyMode,
) -> AutomationLevel:
    """Resolve the effective automation mode from level and apply_mode.

    Raises ValueError if apply_mode is more aggressive than automation_level.
    """
    _LEVEL_ORDER: dict[AutomationLevel, int] = {
        "off": 0,
        "propose": 1,
        "supervised": 2,
        "autopilot": 3,
    }
    if apply_mode == "inherit":
        return automation_level
    apply_order = _LEVEL_ORDER.get(apply_mode, 2)
    level_order = _LEVEL_ORDER[automation_level]
    if apply_order > level_order:
        msg = (
            f"DISCOVERY_APPLY_MODE '{apply_mode}' cannot exceed "
            f"AUTOMATION_LEVEL '{automation_level}'"
        )
        raise ValueError(msg)
    return apply_mode  # type: ignore[return-value]


def _effective_threshold(effective_mode: AutomationLevel) -> float:
    """Return the confidence threshold for auto-apply."""
    if effective_mode == "autopilot":
        return _AUTOPILOT_THRESHOLD
    return _SUPERVISED_THRESHOLD


class AutomationPolicy:
    """Pure-function automation decision engine.

    Evaluates each proposed change item against the current automation
    settings and determines whether it can be auto-applied or requires
    human review.
    """

    def __init__(
        self,
        automation_level: AutomationLevel = "supervised",
        apply_mode: ApplyMode = "inherit",
        app_env: str = "local",
    ):
        self.automation_level: AutomationLevel = automation_level
        self.apply_mode: ApplyMode = apply_mode
        self.app_env = app_env
        self._effective_mode = _resolve_effective_mode(
            automation_level, apply_mode
        )

    def evaluate(
        self,
        change_type: ChangeType,
        confidence: float | None = None,
        *,
        auth_known: bool = True,
        cross_validated: bool = False,
        metadata_complete: bool = True,
    ) -> DecisionItem:
        """Evaluate a single proposal change item.

        Args:
            change_type: Type of config change proposed.
            confidence: Discovery confidence (0.0–1.0).
            auth_known: Whether auth config for this backend is fully known.
            cross_validated: Whether the finding was cross-validated by
                multiple sources (e.g., K8s + Prometheus labels).
            metadata_complete: Whether metric metadata (type, unit) is
                present and valid.

        Returns:
            DecisionItem with outcome and reasoning.
        """
        if self._effective_mode == "off":
            return DecisionItem(
                change_type=change_type,
                outcome="record_only",
                reason="AUTOMATION_LEVEL=off — all proposals are record-only",
                confidence=confidence,
            )

        # Never auto-apply dangerous change types.
        if change_type in _NEVER_AUTO_APPLY:
            return DecisionItem(
                change_type=change_type,
                outcome="rejected",
                reason=f"Change type '{change_type}' is never auto-applied",
                confidence=confidence,
            )

        # Backend URL special handling — production always requires review.
        if change_type == "backend_url":
            return self._evaluate_backend_url(confidence, auth_known)

        # Auth config — requires review regardless.
        if change_type == "auth_config":
            return self._evaluate_auth_config(confidence, auth_known)

        # Service label / metric mapping follow standard thresholds.
        return self._evaluate_standard(
            change_type, confidence, cross_validated, metadata_complete
        )

    def _evaluate_backend_url(
        self,
        confidence: float | None,
        auth_known: bool,
    ) -> DecisionItem:
        if self.app_env == "production":
            return DecisionItem(
                change_type="backend_url",
                outcome="requires_review",
                reason=(
                    "APP_ENV=production — backend URL discovery "
                    "always requires human review"
                ),
                confidence=confidence,
            )

        if not auth_known:
            return DecisionItem(
                change_type="backend_url",
                outcome="requires_review",
                reason="Backend auth configuration is unknown",
                confidence=confidence,
            )

        if confidence is None or confidence < _effective_threshold(
            self._effective_mode
        ):
            return DecisionItem(
                change_type="backend_url",
                outcome="requires_review",
                reason="Confidence below auto-apply threshold",
                confidence=confidence,
            )

        return DecisionItem(
            change_type="backend_url",
            outcome="auto_apply",
            reason="Local environment with sufficient confidence",
            confidence=confidence,
        )

    def _evaluate_auth_config(
        self,
        confidence: float | None,
        auth_known: bool,
    ) -> DecisionItem:
        if not auth_known:
            return DecisionItem(
                change_type="auth_config",
                outcome="requires_review",
                reason="Auth configuration is not fully known",
                confidence=confidence,
            )
        return DecisionItem(
            change_type="auth_config",
            outcome="requires_review",
            reason="Auth config changes always require review",
            confidence=confidence,
        )

    def _evaluate_standard(
        self,
        change_type: ChangeType,
        confidence: float | None,
        cross_validated: bool,
        metadata_complete: bool,
    ) -> DecisionItem:
        if self._effective_mode == "propose":
            return DecisionItem(
                change_type=change_type,
                outcome="record_only",
                reason="AUTOMATION_LEVEL=propose — proposals are record-only",
                confidence=confidence,
            )

        if not metadata_complete:
            return DecisionItem(
                change_type=change_type,
                outcome="requires_review",
                reason="Metadata is incomplete or missing",
                confidence=confidence,
            )

        if confidence is None:
            return DecisionItem(
                change_type=change_type,
                outcome="requires_review",
                reason="No confidence score available",
                confidence=confidence,
            )

        threshold = _effective_threshold(self._effective_mode)

        if confidence >= threshold:
            # Cross-validated findings auto-apply at threshold.
            if cross_validated:
                return DecisionItem(
                    change_type=change_type,
                    outcome="auto_apply",
                    reason=(
                        f"Confidence ({confidence:.0%}) meets threshold "
                        "with cross-validation"
                    ),
                    confidence=confidence,
                )

            # Well above threshold — auto-apply without cross-validation.
            if confidence >= threshold + 0.10:
                return DecisionItem(
                    change_type=change_type,
                    outcome="auto_apply",
                    reason=f"Very high confidence ({confidence:.0%})",
                    confidence=confidence,
                )

            return DecisionItem(
                change_type=change_type,
                outcome="requires_review",
                reason=(
                    f"Confidence ({confidence:.0%}) meets threshold but "
                    "lacks cross-validation"
                ),
                confidence=confidence,
            )

        return DecisionItem(
            change_type=change_type,
            outcome="requires_review",
            reason=f"Confidence ({confidence:.0%}) below threshold ({threshold:.0%})",
            confidence=confidence,
        )

    def evaluate_all(
        self,
        items: list[tuple[ChangeType, float | None]],
        **kwargs: bool,
    ) -> AutomationDecision:
        """Evaluate multiple proposal items and produce aggregate decision.

        The overall outcome is the most conservative across all items:
        rejected > requires_review > detected_only > auto_apply > record_only.
        """
        decisions: list[DecisionItem] = []
        for change_type, confidence in items:
            decision = self.evaluate(change_type, confidence, **kwargs)
            decisions.append(decision)

        overall = max(
            decisions,
            key=lambda d: _OUTCOME_PRIORITY.get(d.outcome, 0),
        )

        return AutomationDecision(
            items=decisions,
            overall_outcome=overall.outcome,
        )
