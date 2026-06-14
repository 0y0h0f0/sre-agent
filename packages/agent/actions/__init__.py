"""Action capability metadata for deterministic remediation safety checks."""

from packages.agent.actions.capabilities import (
    ACTION_CAPABILITIES,
    DB_READ_ONLY_DIAGNOSTIC_OPERATIONS,
    ActionCapability,
    capabilities_by_category,
    get_action_capability,
    iter_action_capabilities,
    known_action_types,
)

__all__ = [
    "ACTION_CAPABILITIES",
    "DB_READ_ONLY_DIAGNOSTIC_OPERATIONS",
    "ActionCapability",
    "capabilities_by_category",
    "get_action_capability",
    "iter_action_capabilities",
    "known_action_types",
]
