"""Public identifier helpers."""

from __future__ import annotations

from uuid import uuid4


def new_id(prefix: str) -> str:
    """Return a public id with the required prefix."""
    clean_prefix = prefix.strip()
    # Requiring a trailing underscore keeps IDs visually parseable and aligns
    # with documented public prefixes such as inc_, run_, act_, and eval_.
    if not clean_prefix or not clean_prefix.endswith("_"):
        msg = "id prefix must be non-empty and end with '_'"
        raise ValueError(msg)
    # UUID4 randomness is sufficient for public identifiers; ordering and
    # internal relational identity stay in database primary keys.
    return f"{clean_prefix}{uuid4().hex}"
