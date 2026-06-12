"""Redis distributed lock with owner-token safety.

Used by DiscoveryRunner tasks and Alertmanager poll tasks to prevent
concurrent execution. Release uses compare-and-delete to prevent
non-owner release.

Usage (context manager)::

    import redis
    r = redis.Redis(...)
    lock = RedisLock(r, "discovery:runner", ttl=300)
    try:
        with lock:
            # do work
    except LockNotAcquiredError:
        # another instance holds the lock
"""

from __future__ import annotations

import time
import uuid
from contextlib import AbstractContextManager
from typing import Any


class LockNotAcquiredError(RuntimeError):
    """Raised when a Redis lock cannot be acquired."""


class LockReleaseError(RuntimeError):
    """Raised when lock release fails (e.g., non-owner attempting release)."""


class RedisLock(AbstractContextManager["RedisLock"]):
    """Redis-based distributed lock with owner-token safety.

    Acquire sets a key with NX (only if not exists) and PX (TTL in ms).
    Release uses a Lua script to compare-and-delete: only the owner
    that holds the correct token can release the lock.

    This prevents:
    - Accidental release by a crashed-then-restarted worker.
    - Lock expiry + re-acquisition then release by original owner.
    """

    _RELEASE_SCRIPT = """
    if redis.call("GET", KEYS[1]) == ARGV[1] then
        return redis.call("DEL", KEYS[1])
    else
        return 0
    end
    """

    def __init__(
        self,
        client: Any,  # redis.Redis
        key: str,
        ttl: int = 300,
        *,
        blocking: bool = False,
        block_timeout: float | None = None,
        retry_interval: float = 0.1,
    ) -> None:
        self._client = client
        self._key = key
        self._ttl = ttl
        self._blocking = blocking
        self._block_timeout = block_timeout
        self._retry_interval = retry_interval
        self._token: str | None = None
        self._acquired = False
        self._release_script: Any = None

    @property
    def token(self) -> str | None:
        return self._token

    @property
    def acquired(self) -> bool:
        return self._acquired

    @property
    def lock_key(self) -> str:
        return self._key

    def acquire(self) -> bool:
        """Try to acquire the lock.

        Returns:
            True if the lock was acquired, False otherwise.
            Raises LockNotAcquiredError if blocking mode times out.
        """
        if self._acquired:
            return True

        self._token = uuid.uuid4().hex
        deadline = None
        if self._blocking and self._block_timeout:
            deadline = time.monotonic() + self._block_timeout

        while True:
            ok = self._client.set(
                self._key, self._token, nx=True, px=self._ttl * 1000
            )
            if ok:
                self._acquired = True
                return True

            if not self._blocking:
                self._token = None
                return False

            if deadline is not None and time.monotonic() >= deadline:
                self._token = None
                raise LockNotAcquiredError(
                    f"Could not acquire lock '{self._key}' "
                    f"within {self._block_timeout}s"
                )

            time.sleep(self._retry_interval)

    def release(self) -> bool:
        """Release the lock using compare-and-delete.

        Returns:
            True if the lock was released, False if it was already
            held by another owner (or already expired).
        """
        if not self._acquired or self._token is None:
            return False

        # Lazy-load the release script.
        if self._release_script is None:
            self._release_script = self._client.register_script(
                self._RELEASE_SCRIPT
            )

        result = self._release_script(keys=[self._key], args=[self._token])
        released = bool(result)
        if released:
            self._acquired = False
            self._token = None
        return released

    def __enter__(self) -> RedisLock:
        ok = self.acquire()
        if not ok:
            raise LockNotAcquiredError(
                f"Lock '{self._key}' is held by another process"
            )
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> None:
        self.release()
        return None


def build_lock_key(prefix: str, *parts: str) -> str:
    """Build a namespaced Redis lock key.

    Example:
        build_lock_key("discovery", "runner", "default")
        # → "lock:discovery:runner:default"
    """
    return "lock:" + ":".join([prefix] + list(parts))
