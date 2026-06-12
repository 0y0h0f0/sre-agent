"""Runbook action step classifier — M9 PR 9.2.

Parses runbook content to identify action steps and classifies each step
into a safety category: read_only, diagnostic_only, approval_required,
forbidden, unknown.

Forbidden/unknown actions must block approval. Approval-required actions
require second confirmation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class ActionClassification(StrEnum):
    READ_ONLY = "read_only"
    DIAGNOSTIC_ONLY = "diagnostic_only"
    APPROVAL_REQUIRED = "approval_required"
    FORBIDDEN = "forbidden"
    UNKNOWN = "unknown"


@dataclass
class ActionStep:
    index: int
    text: str
    classification: ActionClassification = ActionClassification.UNKNOWN
    matched_keywords: list[str] = field(default_factory=list)


# Keywords that immediately classify an action as FORBIDDEN.
_FORBIDDEN_KEYWORDS: set[str] = {
    "delete", "drop", "truncate", "flush", "modify_database",
    "destroy", "purge", "remove_all",
}

# Keywords that classify an action as APPROVAL_REQUIRED (unless forbidden).
_APPROVAL_KEYWORDS: set[str] = {
    "restart", "scale", "rollback", "revert", "redeploy",
    "drain", "cord", "uncord", "reschedule", "evict",
    "enable_rate_limit", "cancel_deployment", "failover",
}

# Keywords that classify an action as DIAGNOSTIC_ONLY.
_DIAGNOSTIC_KEYWORDS: set[str] = {
    "profile", "dump", "trace", "debug", "inspect",
    "thread_dump", "heap_dump", "stack_trace",
}

# Keywords that classify an action as READ_ONLY.
_READ_ONLY_KEYWORDS: set[str] = {
    "check", "query", "view", "list", "get", "describe",
    "review", "verify", "inspect", "monitor", "observe",
    "look", "examine", "read", "show", "display", "fetch",
}


def _extract_action_text(text: str) -> list[str]:
    """Extract action step texts from runbook content."""
    actions: list[str] = []
    # Look for action sections
    action_section = re.search(
        r"##\s*Actions?\s*\n+(.+?)(?=\n##\s|\Z)",
        text,
        re.DOTALL | re.IGNORECASE,
    )
    if action_section:
        # Split on numbered or bulleted items
        items = re.split(r"\n\s*(?:\d+\.\s+|[-*]\s+)", action_section.group(1))
        for item in items:
            stripped = item.strip()
            if stripped and len(stripped) > 3:
                actions.append(stripped)
    return actions


class RunbookActionClassifier:
    """Classify action steps extracted from runbook content."""

    def classify(self, content: str) -> list[ActionStep]:
        """Extract and classify action steps from *content*.

        Args:
            content: Generated runbook content in Markdown.

        Returns:
            List of ActionStep instances with safety classifications.
        """
        texts = _extract_action_text(content)
        steps: list[ActionStep] = []
        for i, text in enumerate(texts, start=1):
            steps.append(self._classify_single(i, text))
        return steps

    def _classify_single(self, index: int, text: str) -> ActionStep:
        lower = text.lower()
        matched: list[str] = []

        # Forbidden check first (highest priority).
        for kw in _FORBIDDEN_KEYWORDS:
            if kw in lower:
                matched.append(kw)
        if matched:
            return ActionStep(
                index=index,
                text=text,
                classification=ActionClassification.FORBIDDEN,
                matched_keywords=matched,
            )

        # Approval required.
        for kw in _APPROVAL_KEYWORDS:
            if kw in lower:
                matched.append(kw)
        if matched:
            return ActionStep(
                index=index,
                text=text,
                classification=ActionClassification.APPROVAL_REQUIRED,
                matched_keywords=matched,
            )

        # Diagnostic only.
        for kw in _DIAGNOSTIC_KEYWORDS:
            if kw in lower:
                matched.append(kw)
        if matched:
            return ActionStep(
                index=index,
                text=text,
                classification=ActionClassification.DIAGNOSTIC_ONLY,
                matched_keywords=matched,
            )

        # Read only.
        for kw in _READ_ONLY_KEYWORDS:
            if kw in lower:
                matched.append(kw)
        if matched:
            return ActionStep(
                index=index,
                text=text,
                classification=ActionClassification.READ_ONLY,
                matched_keywords=matched,
            )

        return ActionStep(
            index=index,
            text=text,
            classification=ActionClassification.UNKNOWN,
        )

    # pylint: disable=no-self-use
    def classification_summary(self, steps: list[ActionStep]) -> dict[str, Any]:
        """Produce a JSON-serializable summary of classifications.

        Args:
            steps: Classified action steps.

        Returns:
            Dict with 'steps' (list of dicts) and 'counts' (per-classification counts).
        """
        counts: dict[str, int] = {
            ActionClassification.READ_ONLY.value: 0,
            ActionClassification.DIAGNOSTIC_ONLY.value: 0,
            ActionClassification.APPROVAL_REQUIRED.value: 0,
            ActionClassification.FORBIDDEN.value: 0,
            ActionClassification.UNKNOWN.value: 0,
        }
        step_dicts: list[dict[str, Any]] = []
        for step in steps:
            counts[step.classification.value] += 1
            step_dicts.append({
                "index": step.index,
                "text": step.text[:200],  # Truncate for storage
                "classification": step.classification.value,
                "matched_keywords": step.matched_keywords,
            })
        return {"steps": step_dicts, "counts": counts}
