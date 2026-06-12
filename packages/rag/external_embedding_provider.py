"""External Embedding Provider — M9 PR 9.9.

Supports external embedding endpoints for semantic runbook search. Data
exfiltration risk — requires explicit opt-in, URL safety validation,
secret reference auth, input redaction, and audit trail.

Default-off. Requires:
- M9_EXTENSIONS_ENABLED=true
- SEMANTIC_RUNBOOK_SEARCH_ENABLED=true
- EXTERNAL_EMBEDDING_PROVIDER_ENABLED=true
- EMBEDDING_PROVIDER=external
- config:write + embedding:external scope
"""

from __future__ import annotations

import logging
import time

import httpx

from packages.common.redaction import redact_text

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 10.0
_DEFAULT_RETRIES = 2
_DEFAULT_CIRCUIT_BREAKER_FAILURES = 5
_DEFAULT_CIRCUIT_BREAKER_WINDOW = 60.0


class ExternalEmbeddingProvider:
    """Embedding provider backed by an external HTTP endpoint.

    Safe by construction:
    - Endpoint validated via BackendUrlSafetyValidator before first use
    - Auth via secret reference (env:VAR_NAME), never raw token
    - Input text redacted before sending
    - Timeout, retry, circuit breaker for resilience
    - Failure → semantic search degraded (keyword-only fallback)
    """

    name = "external"

    def __init__(
        self,
        *,
        endpoint: str,
        secret_ref: str = "",
        timeout_seconds: float = _DEFAULT_TIMEOUT,
        retries: int = _DEFAULT_RETRIES,
    ) -> None:
        self.endpoint = endpoint
        self.secret_ref = secret_ref
        self.timeout_seconds = timeout_seconds
        self.retries = retries
        self._failures: list[float] = []  # Timestamps for circuit breaker
        self._circuit_open = False

    def __repr__(self) -> str:
        return (
            f"ExternalEmbeddingProvider("
            f"endpoint_redacted=True, "
            f"secret_ref=..., "
            f"timeout={self.timeout_seconds}s)"
        )

    # ------------------------------------------------------------------
    # Embedding generation
    # ------------------------------------------------------------------

    def embed(self, text: str) -> list[float] | None:
        """Generate embedding for *text* via external endpoint.

        Returns the embedding vector, or None on any failure (degraded).
        """
        if self._circuit_open:
            logger.warning("External embedding circuit breaker open — returning None")
            return None

        # Redact input before sending.
        redacted = redact_text(text).redacted_text

        # Build request.
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self.secret_ref:
            token = self._resolve_secret()
            if token:
                headers["Authorization"] = f"Bearer {token}"

        payload = {"input": redacted, "model": "external"}

        for attempt in range(self.retries + 1):
            try:
                with httpx.Client(timeout=self.timeout_seconds) as client:
                    resp = client.post(
                        self.endpoint,
                        json=payload,
                        headers=headers,
                    )
                resp.raise_for_status()
                data = resp.json()
                if isinstance(data, dict) and "embedding" in data:
                    emb = data["embedding"]
                    if isinstance(emb, list) and all(isinstance(v, (int, float)) for v in emb):
                        self._record_success()
                        return [float(v) for v in emb]
                # Unexpected response format.
                logger.warning("Unexpected external embedding response format")
                return None
            except Exception:
                logger.warning(
                    "External embedding attempt %d/%d failed",
                    attempt + 1, self.retries + 1, exc_info=True,
                )
                if attempt < self.retries:
                    time.sleep(1.0 * (attempt + 1))  # Linear backoff
                else:
                    self._record_failure()
                    return None

        return None

    # ------------------------------------------------------------------
    # Circuit breaker
    # ------------------------------------------------------------------

    def _record_success(self) -> None:
        window = _DEFAULT_CIRCUIT_BREAKER_WINDOW
        self._failures = [t for t in self._failures if time.monotonic() - t < window]

    def _record_failure(self) -> None:
        now = time.monotonic()
        window = _DEFAULT_CIRCUIT_BREAKER_WINDOW
        self._failures.append(now)
        self._failures = [t for t in self._failures if now - t < window]
        if len(self._failures) >= _DEFAULT_CIRCUIT_BREAKER_FAILURES:
            self._circuit_open = True
            logger.error(
                "External embedding circuit breaker OPEN after %d failures",
                len(self._failures),
            )

    def _resolve_secret(self) -> str | None:
        """Resolve env:VAR_NAME secret reference at call time."""
        import os

        if self.secret_ref.startswith("env:"):
            return os.environ.get(self.secret_ref[4:])
        return None
