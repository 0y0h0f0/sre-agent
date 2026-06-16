"""Redis-based rate limiting for API endpoints.

Uses sorted-set sliding-window counting. Designed as a FastAPI dependency.
Each request is tracked by a key (api_key_id or client IP); when count
exceeds the configured max within the window, subsequent requests are
rejected with HTTP 429.
"""

from __future__ import annotations

import time
from collections import defaultdict
from typing import Any

import redis
from fastapi import Depends

from apps.api.dependencies import get_app_settings
from packages.common.settings import Settings


class RateLimiter:
    """Sliding-window rate limiter backed by a Redis sorted set."""

    def __init__(
        self,
        redis_url: str,
        *,
        max_requests: int = 10,
        window_seconds: int = 60,
        socket_timeout: float = 1.0,
    ) -> None:
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.redis: Any | None = None
        self._memory = redis_url.startswith("memory://")
        self._memory_windows: dict[str, list[float]] = defaultdict(list)
        if not self._memory:
            self.redis = redis.Redis.from_url(
                redis_url,
                socket_connect_timeout=socket_timeout,
                socket_timeout=socket_timeout,
            )

    def is_allowed(self, scope: str, identifier: str) -> bool:
        """Return True when *identifier* has not exceeded the limit for *scope*.

        The Redis key is ``ratelimit:{scope}:{identifier}``.  Each request
        timestamp is added to a sorted set; expired entries are trimmed
        atomically inside a pipeline.
        """
        key = f"ratelimit:{scope}:{identifier}"
        now = time.time()
        window_start = now - self.window_seconds
        if self._memory:
            return self._memory_is_allowed(key, now, window_start)

        try:
            if self.redis is None:
                return True
            pipe = self.redis.pipeline(transaction=True)
            pipe.zremrangebyscore(key, 0, window_start)  # trim expired
            pipe.zcard(key)                               # count remaining
            pipe.zadd(key, {str(now): now})               # add current
            pipe.expire(key, self.window_seconds + 5)     # TTL (slop for races)
            _, count, _, _ = pipe.execute()
            return int(count) < self.max_requests  # type: ignore[arg-type]
        except redis.RedisError:
            # Fail open: if Redis is unreachable, allow the request through
            # rather than blocking legitimate traffic.
            import logging
            logging.getLogger(__name__).warning(
                "rate limiter Redis error — allowing request", exc_info=True
            )
            return True

    def _memory_is_allowed(self, key: str, now: float, window_start: float) -> bool:
        window = [
            timestamp
            for timestamp in self._memory_windows.get(key, [])
            if timestamp > window_start
        ]
        allowed = len(window) < self.max_requests
        window.append(now)
        self._memory_windows[key] = window
        return allowed


def build_rate_limiter(
    settings: Settings = Depends(get_app_settings),
) -> RateLimiter:
    """Create a RateLimiter from application settings."""
    return RateLimiter(
        redis_url=settings.redis_url,
        max_requests=settings.rate_limit_max_requests,
        window_seconds=settings.rate_limit_window_seconds,
    )
