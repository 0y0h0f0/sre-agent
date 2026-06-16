from __future__ import annotations

from apps.api.rate_limit import build_rate_limiter
from packages.common.settings import Settings


def test_build_rate_limiter_uses_injected_settings() -> None:
    settings = Settings(
        redis_url="redis://example.invalid:6379/9",
        rate_limit_max_requests=123,
        rate_limit_window_seconds=7,
    )

    limiter = build_rate_limiter(settings)

    assert limiter.max_requests == 123
    assert limiter.window_seconds == 7
