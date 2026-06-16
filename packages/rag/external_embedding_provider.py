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
from collections.abc import Callable, Iterable

import httpx

from packages.common.backend_url_safety import BackendUrlSafetyValidator
from packages.common.errors import ValidationAppError
from packages.common.redaction import redact_text

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 10.0
_DEFAULT_RETRIES = 2
_DEFAULT_CIRCUIT_BREAKER_FAILURES = 5
_DEFAULT_CIRCUIT_BREAKER_WINDOW = 60.0
_PRIMARY_STORE_DIMENSION = 512


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
    dimension = _PRIMARY_STORE_DIMENSION
    model_name = "external-512"

    def __init__(
        self,
        *,
        endpoint: str,
        secret_ref: str = "",
        timeout_seconds: float = _DEFAULT_TIMEOUT,
        retries: int = _DEFAULT_RETRIES,
        app_env: str = "local",
        allowed_domain_patterns: list[str] | None = None,
        blocked_domain_patterns: list[str] | None = None,
        dns_resolver: Callable[[str], Iterable[str]] | None = None,
        url_validator: BackendUrlSafetyValidator | None = None,
    ) -> None:
        if app_env == "production" and not allowed_domain_patterns and url_validator is None:
            raise ValidationAppError(
                "external embedding endpoint requires an allowlist in production",
                details={"required_setting": "EXTERNAL_EMBEDDING_ALLOWED_DOMAINS"},
            )
        validator = url_validator or BackendUrlSafetyValidator(
            app_env=app_env,
            allowed_domain_patterns=allowed_domain_patterns,
            blocked_domain_patterns=blocked_domain_patterns,
            require_https=app_env == "production",
            strict_private_networks=app_env != "local",
            block_cluster_internal_domains=app_env != "local",
            resolve_dns=app_env == "production",
            dns_resolver=dns_resolver,
        )
        validation = validator.validate(endpoint)
        if not validation.is_safe:
            raise ValidationAppError(
                "unsafe external embedding endpoint",
                details={"reason": validation.reason},
            )
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

    def embed_text(self, text: str) -> list[float]:
        vector = self.embed(text)
        if vector is None or len(vector) != self.dimension:
            return [0.0] * self.dimension
        return vector

    def embed_many(self, texts: list[str]) -> list[list[float]]:
        return [self.embed_text(text) for text in texts]

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
