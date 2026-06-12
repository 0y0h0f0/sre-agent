"""BackendAuthConfig — runtime-only and redacted auth for observability backends.

Phase 0-8 supports ``env:VAR_NAME`` secret references only.
Raw secrets never enter DB, audit, debug log, AgentDeps, LLM prompt, or state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

AuthType = Literal["none", "bearer", "basic", "mtls"]


@dataclass
class RedactedBackendAuthConfig:
    """Safe-to-log representation of backend auth config (no raw secrets)."""

    auth_type: AuthType = "none"
    has_token: bool = False
    has_password: bool = False
    username: str | None = None
    cert_ref: str | None = None
    tls_verify: bool = True
    tls_server_name: str | None = None
    extra_safe_headers: dict[str, str] = field(default_factory=dict)

    def to_safe_dict(self) -> dict[str, Any]:
        """Return a dict safe for audit log, AgentDeps, and LLM state."""
        return {
            "auth_type": self.auth_type,
            "has_token": self.has_token,
            "has_password": self.has_password,
            "username": self.username,
            "cert_ref": self.cert_ref,
            "tls_verify": self.tls_verify,
            "tls_server_name": self.tls_server_name,
        }


@dataclass
class RuntimeBackendAuthConfig:
    """Runtime auth used ONLY during backend client construction.

    Raw secrets resolved from ``env:VAR_NAME`` references at construction time.
    MUST NOT be serialized into AgentDeps, state, audit, logs, or LLM prompts.
    """

    auth_type: AuthType = "none"

    # Bearer token.
    token: str | None = None
    token_env_var: str | None = None

    # Basic auth.
    username: str | None = None
    password: str | None = None

    # mTLS.
    cert_file: str | None = None
    key_file: str | None = None
    ca_file: str | None = None

    # TLS.
    tls_verify: bool = True
    tls_server_name: str | None = None
    timeout_seconds: float = 10.0

    def redacted(self) -> RedactedBackendAuthConfig:
        """Return a redacted (safe-to-log) representation."""
        return RedactedBackendAuthConfig(
            auth_type=self.auth_type,
            has_token=self.token is not None or self.token_env_var is not None,
            has_password=self.password is not None,
            username=self.username,
            cert_ref=self.cert_file,
            tls_verify=self.tls_verify,
            tls_server_name=self.tls_server_name,
        )


def resolve_secret_ref(ref: str) -> str | None:
    """Resolve a secret reference (Phase 0-8: ``env:VAR_NAME`` only).

    Returns the env var value or None if not set.
    """
    import os

    if ref.startswith("env:"):
        return os.environ.get(ref[4:])
    return None
