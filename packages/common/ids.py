"""Public identifier helpers."""

from __future__ import annotations

from uuid import uuid4


def new_id(prefix: str) -> str:
    """Return a public id with the required prefix."""
    clean_prefix = prefix.strip()
    if not clean_prefix or not clean_prefix.endswith("_"):
        msg = "id prefix must be non-empty and end with '_'"
        raise ValueError(msg)
    return f"{clean_prefix}{uuid4().hex}"
