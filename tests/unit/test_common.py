from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from packages.common.ids import new_id
from packages.common.time import ensure_utc, utc_now


def test_new_id_uses_prefix() -> None:
    public_id = new_id("inc_")
    assert public_id.startswith("inc_")
    assert len(public_id) > len("inc_")


def test_new_id_rejects_bad_prefix() -> None:
    with pytest.raises(ValueError):
        new_id("inc")


def test_utc_now_is_timezone_aware() -> None:
    assert utc_now().tzinfo is not None


def test_ensure_utc_normalizes_naive_and_aware_values() -> None:
    naive = datetime(2026, 6, 1, 8, 0)
    aware = datetime(2026, 6, 1, 16, 0, tzinfo=timezone(timedelta(hours=8)))
    assert ensure_utc(naive).tzinfo == UTC
    assert ensure_utc(aware).hour == 8
