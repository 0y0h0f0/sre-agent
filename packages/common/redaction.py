"""Text redaction for M9 safety boundaries.

Removes secrets, tokens, passwords, private keys, auth headers, internal URLs,
IPs, and other sensitive patterns from text before it enters an LLM prompt,
audit log, or any external context.

All redaction functions are deterministic and do NOT depend on external services.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# --- Patterns that MUST be redacted ---

_BEARER_TOKEN_RE = re.compile(
    r"""(?:Bearer|bearer)\s+[A-Za-z0-9\-._~+/]+=*""",
)
_BASIC_AUTH_RE = re.compile(
    r"""Basic\s+[A-Za-z0-9+/]+=*""",
)
_API_KEY_HEADER_RE = re.compile(
    r"""["']?(?:X-?Api-?Key|api_key|apikey)["']?\s*[:=]\s*["']?[^"'\s,}]+["']?""",
    re.IGNORECASE,
)
_SECRET_VALUE_RE = re.compile(
    r"""["']?(?:auth[-_]?token|access[-_]?token|refresh[-_]?token|id[-_]?token|session[-_]?token|token|secret|client[-_]?secret)["']?\s*[:=]\s*["']?[^"'\s,}]+["']?""",
    re.IGNORECASE,
)
# Matches "password": "value", password=value, etc.
_PASSWORD_RE = re.compile(
    r"""["']?(?:password|passwd|pwd)["']?\s*[:=]\s*["']?[^"'\s,}]+["']?""",
    re.IGNORECASE,
)
# Matches private key blocks (PEM format) — lenient to catch truncated/shortened keys
_PRIVATE_KEY_RE = re.compile(
    r"""-----BEGIN\s+(?:RSA|EC|DSA|OPENSSH|ENCRYPTED)\s+PRIVATE\s+KEY-----.*?-----END\s+(?:RSA|EC|DSA|OPENSSH|ENCRYPTED)\s+PRIVATE\s+KEY-----""",
    re.DOTALL,
)
# Internal URLs (localhost, 127.x, ::1, link-local, metadata endpoints)
_INTERNAL_URL_RE = re.compile(
    r"""https?://(?:localhost|127\.\d+\.\d+\.\d+|\[::1\]|169\.254\.\d+\.\d+|metadata\.google\.internal|100\.\d+\.\d+\.\d+|[A-Za-z0-9.-]+(?:\.svc|\.svc\.cluster\.local|\.cluster\.local))(?:[/:][^\s"')\]}>]*|)""",
    re.IGNORECASE,
)
# Private IPs (10.x, 172.16-31.x, 192.168.x)
_PRIVATE_IP_RE = re.compile(
    r"""\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3})\b""",
)
# Raw token patterns (common formats)
_TOKEN_PATTERN_RE = re.compile(
    r"""\b(?:[A-Za-z0-9+/]{40,}={0,2}|sk-[A-Za-z0-9]{32,}|[A-Za-z0-9]{32,}:[\w-]+)\b""",
)
# URL-embedded credentials (user:password@host in URLs)
_URL_CREDENTIAL_RE = re.compile(
    r"""://[^/\s@]+:[^/\s@]+@""",
)
# Kubernetes namespace references (for redaction in prompts)
_NAMESPACE_RE = re.compile(
    r"""\bnamespace\s*[:=]\s*["']?[\w-]+["']?""",
    re.IGNORECASE,
)
# Service/application names in external-search queries can identify internal
# topology, so redact keyed references while preserving generic prose.
_SERVICE_NAME_RE = re.compile(
    r"""\b(?:service|service_name|app|application)\s*[:=]\s*["']?[\w.-]+["']?""",
    re.IGNORECASE,
)


# The replacement text for redacted values.
_REDACTED_PLACEHOLDER = "[REDACTED]"


@dataclass
class RedactionResult:
    """Result of a text redaction operation."""

    redacted_text: str
    redaction_count: int = 0
    redaction_types: list[str] = field(default_factory=list)

    def to_safe_dict(self) -> dict[str, Any]:
        """Return a safe-to-log summary (no raw values)."""
        return {
            "redaction_count": self.redaction_count,
            "redaction_types": sorted(set(self.redaction_types)),
        }


# Ordered list of (name, pattern) pairs — applied in order.
_REDACTION_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("bearer_token", _BEARER_TOKEN_RE),
    ("basic_auth", _BASIC_AUTH_RE),
    ("api_key_header", _API_KEY_HEADER_RE),
    ("secret_value", _SECRET_VALUE_RE),
    ("password", _PASSWORD_RE),
    ("private_key", _PRIVATE_KEY_RE),
    ("url_credential", _URL_CREDENTIAL_RE),
    ("internal_url", _INTERNAL_URL_RE),
    ("private_ip", _PRIVATE_IP_RE),
    ("raw_token", _TOKEN_PATTERN_RE),
    ("namespace", _NAMESPACE_RE),
    ("service_name", _SERVICE_NAME_RE),
]


def redact_text(text: str) -> RedactionResult:
    """Apply all redaction rules to *text* and return the redacted version.

    Args:
        text: Raw text that may contain secrets.

    Returns:
        RedactionResult with redacted text and metadata about what was redacted.
    """
    result = text
    count = 0
    types: list[str] = []

    for name, pattern in _REDACTION_RULES:
        matches = pattern.findall(result)
        if matches:
            result = pattern.sub(_REDACTED_PLACEHOLDER, result)
            count += len(matches)
            types.append(name)

    return RedactionResult(
        redacted_text=result,
        redaction_count=count,
        redaction_types=types,
    )


def redact_dict_values(data: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """Recursively redact string values in a dict.

    Returns (redacted_dict, total_redactions).
    """
    total = 0
    result: dict[str, Any] = {}
    for key, value in data.items():
        if isinstance(value, str):
            rr = redact_text(value)
            result[key] = rr.redacted_text
            total += rr.redaction_count
        elif isinstance(value, dict):
            inner, inner_count = redact_dict_values(value)
            result[key] = inner
            total += inner_count
        elif isinstance(value, list):
            cleaned, list_count = _redact_list(value)
            result[key] = cleaned
            total += list_count
        else:
            result[key] = value
    return result, total


def _redact_list(items: list[Any]) -> tuple[list[Any], int]:
    total = 0
    result: list[Any] = []
    for item in items:
        if isinstance(item, str):
            rr = redact_text(item)
            result.append(rr.redacted_text)
            total += rr.redaction_count
        elif isinstance(item, dict):
            inner_dict, inner_count = redact_dict_values(item)
            result.append(inner_dict)
            total += inner_count
        elif isinstance(item, list):
            inner_list, inner_count = _redact_list(item)
            result.append(inner_list)
            total += inner_count
        else:
            result.append(item)
    return result, total
