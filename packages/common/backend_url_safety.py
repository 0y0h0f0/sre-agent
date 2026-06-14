"""Backend URL Safety Validator — SSRF prevention for backend URLs.

Ensures backend URLs are safe before they enter EffectiveConfig, are published,
or used in worker backend construction.
"""

from __future__ import annotations

import ipaddress
import re
import socket
from collections.abc import Callable, Iterable
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
    "::1/128",
    "169.254.0.0/16",
    "fe80::/10",
    "fc00::/7",
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
_CLUSTER_INTERNAL_SUFFIXES = (
    ".svc",
    ".svc.cluster.local",
    ".cluster.local",
)


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
        blocked_domain_patterns: list[str] | None = None,
        allowed_domain_patterns: list[str] | None = None,
        require_https: bool = False,
        strict_private_networks: bool = False,
        block_cluster_internal_domains: bool = False,
        resolve_dns: bool = False,
        dns_resolver: Callable[[str], Iterable[str]] | None = None,
        k8s_evidence: dict[str, Any] | None = None,
    ):
        self._app_env = app_env
        self._allowlist = allowlist_patterns or []
        self._blocked_domains = blocked_domain_patterns or []
        self._allowed_domains = allowed_domain_patterns or []
        self._require_https = require_https
        self._strict_private_networks = strict_private_networks
        self._block_cluster_internal_domains = block_cluster_internal_domains
        self._resolve_dns = resolve_dns
        self._dns_resolver = dns_resolver
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
        if self._require_https and parsed.scheme != "https":
            return UrlValidationResult(
                url=url,
                is_safe=False,
                reason="HTTPS is required",
            )
        if parsed.username or parsed.password:
            return UrlValidationResult(
                url=url,
                is_safe=False,
                reason="URL credentials are not allowed",
            )

        hostname = parsed.hostname
        if not hostname:
            return UrlValidationResult(
                url=url, is_safe=False, reason="No hostname in URL"
            )
        hostname = hostname.rstrip(".").lower()

        if self._matches_patterns(hostname, self._blocked_domains):
            return UrlValidationResult(
                url=url,
                is_safe=False,
                reason=f"Host '{hostname}' is blocked",
            )

        # Always block metadata endpoints.
        if hostname in _METADATA_HOSTS:
            return UrlValidationResult(
                url=url,
                is_safe=False,
                reason=f"Host '{hostname}' is a metadata endpoint",
            )

        if self._block_cluster_internal_domains and self._is_cluster_internal(hostname):
            return UrlValidationResult(
                url=url,
                is_safe=False,
                reason=f"Host '{hostname}' is a cluster-internal domain",
            )

        strict_network_checks = self._strict_private_networks or self._app_env != "local"

        # Local mode allows localhost.
        if self._app_env == "local" and not self._strict_private_networks:
            return UrlValidationResult(url=url, is_safe=True)

        # Production safety checks.
        if strict_network_checks and hostname in _DEFAULT_BLOCKED_HOSTS:
            if self._strict_private_networks or not self._is_allowlisted(hostname):
                return UrlValidationResult(
                    url=url,
                    is_safe=False,
                    reason=f"Host '{hostname}' blocked in production",
                )
            return UrlValidationResult(
                url=url, is_safe=True, evidence={"allowlisted": True}
            )

        if strict_network_checks and self._is_blocked_ip(hostname):
            if self._strict_private_networks or not self._is_allowlisted(hostname):
                return UrlValidationResult(
                    url=url,
                    is_safe=False,
                    reason=f"IP '{hostname}' in blocked network",
                )
            return UrlValidationResult(
                url=url, is_safe=True, evidence={"allowlisted": True}
            )

        if self._allowed_domains and not self._matches_patterns(
            hostname, self._allowed_domains
        ):
            return UrlValidationResult(
                url=url,
                is_safe=False,
                reason=f"Host '{hostname}' is not allowlisted",
            )

        if self._resolve_dns:
            resolved = self._resolve_host_ips(hostname)
            if not resolved:
                return UrlValidationResult(
                    url=url,
                    is_safe=False,
                    reason=f"DNS resolution failed for host '{hostname}'",
                )
            for ip in resolved:
                if self._is_blocked_ip(ip) or ip in _DEFAULT_BLOCKED_HOSTS:
                    return UrlValidationResult(
                        url=url,
                        is_safe=False,
                        reason=f"DNS for host '{hostname}' resolved to blocked IP",
                        evidence={"resolved_ip": ip},
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
        return self._matches_patterns(hostname, self._allowlist)

    @staticmethod
    def _matches_patterns(hostname: str, patterns: list[str]) -> bool:
        normalized = hostname.rstrip(".").lower()
        for pattern in patterns:
            p = pattern.strip()
            if not p:
                continue
            p = p.rstrip(".").lower()
            regex = "^" + re.escape(p).replace(r"\*", ".*") + "$"
            if re.match(regex, normalized):
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

    @staticmethod
    def _is_cluster_internal(hostname: str) -> bool:
        return any(hostname.endswith(suffix) for suffix in _CLUSTER_INTERNAL_SUFFIXES)

    def _resolve_host_ips(self, hostname: str) -> list[str] | None:
        try:
            ipaddress.ip_address(hostname)
            return [hostname]
        except ValueError:
            pass

        try:
            if self._dns_resolver is not None:
                return [str(ip) for ip in self._dns_resolver(hostname)]
            infos = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
        except Exception:
            return None

        ips: set[str] = set()
        for info in infos:
            sockaddr = info[4]
            if sockaddr:
                ips.add(str(sockaddr[0]))
        return sorted(ips)

    def _is_k8s_allowed(self, hostname: str) -> bool:
        if not self._k8s_evidence:
            return False
        for svc in self._k8s_evidence.get("services", []):
            dns = svc.get("dns_name", "")
            if dns and hostname.endswith(dns):
                return True
        return False
