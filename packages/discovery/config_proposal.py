"""ConfigProposalGenerator — compare discovery results with current config.

M3 PR 3.5: Generates config diffs from DiscoveryResult, evaluates each change
via AutomationPolicy, and produces DiscoveryProposal items.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from packages.discovery.automation_policy import (
    AutomationPolicy,
    ChangeType,
    DecisionOutcome,
)
from packages.discovery.models import DiscoveryResult


@dataclass
class ConfigDiffItem:
    """A single configuration change proposal."""

    config_key: str
    change_type: ChangeType
    old_value: Any | None = None
    new_value: Any | None = None
    action: str = "add"  # add | update | delete
    confidence: float | None = None
    evidence: dict[str, Any] = field(default_factory=dict)
    decision: DecisionOutcome = "requires_review"
    decision_reason: str = ""


@dataclass
class ConfigProposal:
    """Aggregated config proposal from a discovery run."""

    run_id: str
    items: list[ConfigDiffItem] = field(default_factory=list)
    overall_decision: DecisionOutcome = "record_only"
    ready_to_publish: bool = False

    @property
    def has_changes(self) -> bool:
        return len(self.items) > 0


class ConfigProposalGenerator:
    """Compares discovery results with current config and generates proposals.

    Usage::

        generator = ConfigProposalGenerator(policy=AutomationPolicy())
        proposal = generator.generate(discovery_result, current_config)
    """

    def __init__(
        self,
        policy: AutomationPolicy | None = None,
    ) -> None:
        self._policy = policy or AutomationPolicy()

    def generate(
        self,
        result: DiscoveryResult,
        current_config: dict[str, Any] | None = None,
    ) -> ConfigProposal:
        """Generate a config proposal from a DiscoveryResult.

        Args:
            result: The DiscoveryResult to generate proposals from.
            current_config: The current effective config (from DB or env).

        Returns:
            ConfigProposal with diff items and automation decisions.
        """
        config = current_config or {}
        items: list[ConfigDiffItem] = []

        # --- Backend URL changes ---
        items.extend(self._propose_backend_urls(result, config))

        # --- Service label changes ---
        items.extend(self._propose_service_labels(result, config))

        # --- Metric mapping changes ---
        items.extend(self._propose_metric_mappings(result, config))

        # Evaluate each item through AutomationPolicy.
        for item in items:
            decision = self._policy.evaluate(
                change_type=item.change_type,
                confidence=item.confidence,
                auth_known=not _is_auth_unknown(item.config_key, result),
            )
            item.decision = decision.outcome
            item.decision_reason = decision.reason

        # Aggregate overall decision.
        if items:
            overall = self._policy.evaluate_all(
                [(it.change_type, it.confidence) for it in items],
            )
            overall_outcome = overall.overall_outcome
            ready = overall.can_auto_apply
        else:
            overall_outcome = "record_only"
            ready = False

        return ConfigProposal(
            run_id=result.run_id,
            items=items,
            overall_decision=overall_outcome,
            ready_to_publish=ready,
        )

    # ------------------------------------------------------------------
    # Proposal methods
    # ------------------------------------------------------------------

    def _propose_backend_urls(
        self,
        result: DiscoveryResult,
        current_config: dict[str, Any],
    ) -> list[ConfigDiffItem]:
        """Propose backend URL changes from discovered endpoints."""
        items: list[ConfigDiffItem] = []
        for ep in result.backend_endpoints:
            key = f"{ep.backend_type}_url"
            current = current_config.get(key)
            new_url = ep.url

            if not new_url:
                continue

            if current and current == new_url:
                continue  # No change.

            items.append(ConfigDiffItem(
                config_key=key,
                change_type="backend_url",
                old_value=current,
                new_value=new_url,
                action="add" if not current else "update",
                confidence=ep.confidence,
                evidence=ep.evidence,
            ))
        return items

    def _propose_service_labels(
        self,
        result: DiscoveryResult,
        current_config: dict[str, Any],
    ) -> list[ConfigDiffItem]:
        """Propose service label changes."""
        items: list[ConfigDiffItem] = []
        # If discovery found a service label different from current.
        # For now, this is informed by the runner's label detection.
        # The runner already stores label results in the capability matrix.
        return items

    def _propose_metric_mappings(
        self,
        result: DiscoveryResult,
        current_config: dict[str, Any],
    ) -> list[ConfigDiffItem]:
        """Propose new metric mappings."""
        items: list[ConfigDiffItem] = []
        for mapping in result.metric_mappings:
            if mapping.status != "available":
                continue
            current_mapping = current_config.get(
                f"metric_{mapping.semantic_type}"
            )
            if current_mapping and current_mapping == mapping.metric_name:
                continue
            items.append(ConfigDiffItem(
                config_key=f"metric_{mapping.semantic_type}",
                change_type="metric_mapping",
                old_value=current_mapping,
                new_value=mapping.metric_name,
                action="add" if not current_mapping else "update",
                confidence=mapping.confidence,
                evidence=mapping.evidence,
            ))
        return items


def _is_auth_unknown(key: str, result: DiscoveryResult) -> bool:
    """Check if auth is unknown for a backend endpoint."""
    for ep in result.backend_endpoints:
        if f"{ep.backend_type}_url" == key:
            return ep.auth_required_unknown
    return False
