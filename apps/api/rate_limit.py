"""Redis-based rate limiting for API endpoints.

Uses sorted-set sliding-window counting. Designed as a FastAPI dependency.
Each request is tracked by a key (api_key_id or client IP); when count
exceeds the configured max within the window, subsequent requests are
rejected with HTTP 429.
"""

from __future__ import annotations

import time

import redis


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
        try:
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


def build_rate_limiter() -> RateLimiter:
    """Create a RateLimiter from application settings."""
    from packages.common.settings import get_settings

    settings = get_settings()
    return RateLimiter(
        redis_url=settings.redis_url,
        max_requests=settings.rate_limit_max_requests,
        window_seconds=settings.rate_limit_window_seconds,
    )
