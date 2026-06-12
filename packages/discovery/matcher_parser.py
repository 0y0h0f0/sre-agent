"""Alertmanager matcher parser, scope validation, and allowlist mapping.

M4 PR 4.2 + 4.3 + 4.4:
- parse_matchers() / to_alertmanager_filter() (PR 4.2)
- AlertPollFilters + has_valid_scope() (PR 4.3)
- _allowlist_to_server_matchers() + can_map_to_server_side() (PR 4.4)
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from pydantic import BaseModel, Field


class InvalidMatcherError(ValueError):
    """Raised when a matcher string is invalid."""


_VALID_LABEL_NAME = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_:]*$")


@dataclass
class Matcher:
    """A parsed Alertmanager label matcher."""

    label: str
    operator: str  # =, !=, =~, !~
    value: str


def parse_matchers(matcher_strings: list[str]) -> list[Matcher]:
    """Parse Alertmanager matcher strings into structured Matcher objects.

    Supports =, !=, =~, !~ operators. Quoted values with internal commas
    are preserved as single values.
    """
    result: list[Matcher] = []
    for raw in matcher_strings:
        raw = raw.strip()
        if not raw:
            continue
        op: str | None = None
        op_pos = -1
        for candidate in ["!~", "=~", "!=", "="]:
            pos = raw.find(candidate)
            if pos > 0:
                op = candidate
                op_pos = pos
                break
        if op is None or op_pos <= 0:
            raise InvalidMatcherError(f"Invalid matcher '{raw}': no valid operator")
        label = raw[:op_pos].strip()
        value = raw[op_pos + len(op):].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        if not _VALID_LABEL_NAME.match(label):
            raise InvalidMatcherError(f"Invalid label name '{label}' in '{raw}'")
        if op in ("=~", "!~"):
            try:
                re.compile(value)
            except re.error as exc:
                raise InvalidMatcherError(
                    f"Invalid regex '{value}' in '{raw}': {exc}"
                ) from exc
        result.append(Matcher(label=label, operator=op, value=value))
    return result


def to_alertmanager_filter(matchers: list[Matcher]) -> list[str]:
    """Convert parsed Matchers to Alertmanager API filter[] strings."""
    return [f"{m.label}{m.operator}{m.value}" for m in matchers]


# ---------------------------------------------------------------------------
# PR 4.3: Scope Validation
# ---------------------------------------------------------------------------


class AlertPollFilters(BaseModel):
    """Filters defining the scope of an Alertmanager poll."""

    receiver: str | None = None
    namespace_allowlist: list[str] = Field(default_factory=list)
    service_allowlist: list[str] = Field(default_factory=list)
    extra_matchers: list[str] = Field(default_factory=list)

    model_config = {"extra": "forbid"}


_EXCLUDED_SCOPE_LABELS = {"severity", "priority"}


def has_valid_scope(filters: AlertPollFilters) -> bool:
    """Check that poll filters define a valid (bounded) scope.

    Requires at least one of: receiver, namespace_allowlist, service_allowlist,
    or a non-severity/non-priority extra matcher.
    """
    if filters.receiver:
        return True
    if filters.namespace_allowlist:
        return True
    if filters.service_allowlist:
        return True
    for raw in filters.extra_matchers:
        raw = raw.strip()
        if not raw:
            continue
        for candidate in ["!~", "=~", "!=", "="]:
            pos = raw.find(candidate)
            if pos > 0:
                label = raw[:pos].strip().lower()
                if label not in _EXCLUDED_SCOPE_LABELS:
                    return True
                break
    return False


# ---------------------------------------------------------------------------
# PR 4.4: Allowlist to Server-side Matcher Mapping
# ---------------------------------------------------------------------------


def _allowlist_to_server_matchers(
    namespace_allowlist: list[str],
    service_allowlist: list[str],
    service_label: str = "service",
) -> list[str]:
    """Convert allowlists to Alertmanager matcher strings for filter[] params."""
    result: list[str] = []
    if namespace_allowlist:
        escaped = "|".join(re.escape(ns) for ns in namespace_allowlist)
        result.append(f'namespace=~"{escaped}"')
    if service_allowlist:
        escaped = "|".join(re.escape(svc) for svc in service_allowlist)
        result.append(f'{service_label}=~"{escaped}"')
    return result


def can_map_to_server_side(
    filters: AlertPollFilters,
    service_label: str = "service",
) -> bool:
    """Check whether the poll scope can be expressed as server-side matchers."""
    has_allowlists = bool(
        filters.namespace_allowlist
        or filters.service_allowlist
        or filters.extra_matchers
    )
    if filters.receiver and not has_allowlists:
        return True
    if filters.namespace_allowlist or filters.service_allowlist:
        return True
    if filters.extra_matchers:
        return True
    return False
