"""Backend URL Safety Validator — SSRF prevention for backend URLs.

Ensures backend URLs are safe before they enter EffectiveConfig, are published,
or used in worker backend construction.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

# Host patterns always rejected in production.
_DEFAULT_BLOCKED_HOSTS: set[str] = {
    "localhost", "127.0.0.1", "::1", "0.0.0.0",
}

# Networks always rejected in production (unless allowlisted).
_DEFAULT_BLOCKED_NETWORKS: list[str] = [
    "127.0.0.0/8",
    "169.254.0.0/16",
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
]

# Known cloud metadata endpoints (always blocked).
_METADATA_HOSTS: set[str] = {
    "169.254.169.254",
    "metadata.google.internal",
    "100.100.100.200",
}

_ALLOWED_SCHEMES = {"http", "https"}


@dataclass
class UrlValidationResult:
    """Result of validating a backend URL."""

    url: str
    is_safe: bool
    reason: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)


class BackendUrlSafetyValidator:
    """Validates backend URLs for safety.

    Production defaults are strict: localhost, link-local, metadata endpoints,
    and private IPs are rejected unless explicitly allowlisted.
    """

    def __init__(
        self,
        *,
        app_env: str = "local",
        allowlist_patterns: list[str] | None = None,
        k8s_evidence: dict[str, Any] | None = None,
    ):
        self._app_env = app_env
        self._allowlist = allowlist_patterns or []
        self._k8s_evidence = k8s_evidence or {}

    def validate(self, url: str | None) -> UrlValidationResult:
        """Validate a backend URL."""
        if not url or not url.strip():
            return UrlValidationResult(
                url=url or "", is_safe=False, reason="URL is empty"
            )

        try:
            parsed = urlparse(url)
        except Exception:
            return UrlValidationResult(
                url=url, is_safe=False, reason="URL parsing failed"
            )

        if parsed.scheme not in _ALLOWED_SCHEMES:
            return UrlValidationResult(
                url=url,
                is_safe=False,
                reason=f"Scheme '{parsed.scheme}' not allowed",
            )

        hostname = parsed.hostname
        if not hostname:
            return UrlValidationResult(
                url=url, is_safe=False, reason="No hostname in URL"
            )

        # Always block metadata endpoints.
        if hostname in _METADATA_HOSTS:
            return UrlValidationResult(
                url=url,
                is_safe=False,
                reason=f"Host '{hostname}' is a metadata endpoint",
            )

        # Local mode allows localhost.
        if self._app_env == "local":
            return UrlValidationResult(url=url, is_safe=True)

        # Production safety checks.
        if hostname in _DEFAULT_BLOCKED_HOSTS:
            if not self._is_allowlisted(hostname):
                return UrlValidationResult(
                    url=url,
                    is_safe=False,
                    reason=f"Host '{hostname}' blocked in production",
                )
            return UrlValidationResult(
                url=url, is_safe=True, evidence={"allowlisted": True}
            )

        if self._is_blocked_ip(hostname):
            if not self._is_allowlisted(hostname):
                return UrlValidationResult(
                    url=url,
                    is_safe=False,
                    reason=f"IP '{hostname}' in blocked network",
                )
            return UrlValidationResult(
                url=url, is_safe=True, evidence={"allowlisted": True}
            )

        if self._is_allowlisted(hostname):
            return UrlValidationResult(
                url=url, is_safe=True, evidence={"allowlisted": True}
            )

        if self._is_k8s_allowed(hostname):
            return UrlValidationResult(
                url=url, is_safe=True, evidence={"k8s_evidence": True}
            )

        return UrlValidationResult(url=url, is_safe=True)

    def _is_allowlisted(self, hostname: str) -> bool:
        for pattern in self._allowlist:
            p = pattern.strip()
            if not p:
                continue
            regex = "^" + re.escape(p).replace(r"\*", ".*") + "$"
            if re.match(regex, hostname):
                return True
        return False

    def _is_blocked_ip(self, hostname: str) -> bool:
        try:
            ip = ipaddress.ip_address(hostname)
        except ValueError:
            return False
        for net_str in _DEFAULT_BLOCKED_NETWORKS:
            if ip in ipaddress.ip_network(net_str):
                return True
        return False

    def _is_k8s_allowed(self, hostname: str) -> bool:
        if not self._k8s_evidence:
            return False
        for svc in self._k8s_evidence.get("services", []):
            dns = svc.get("dns_name", "")
            if dns and hostname.endswith(dns):
                return True
        return False
