"""Deployment/change backends for the GitChangeTool (roadmap Phase 2.1).

The fixture backend preserves MVP behaviour. GitHub and Argo CD backends query
real deployment history and are only contacted when ``deployment_backend`` is
set away from ``fixture``. All backends return change dicts in one shape:

    {service, deployed_at, commit_sha, author, summary, files}
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

import httpx

from packages.common.settings import Settings


class DeploymentBackend(Protocol):
    name: str

    def fetch_changes(self, service: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
        """Return deployment change dicts for the service (newest first)."""


class FixtureDeploymentBackend:
    """Reads deployment changes from the demo fixture (MVP default)."""

    name = "fixture"

    def __init__(self, fixture_path: str | Path = "demo/faults/git_changes.json") -> None:
        self.fixture_path = Path(fixture_path)

    def fetch_changes(self, service: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
        payload = json.loads(self.fixture_path.read_text(encoding="utf-8"))
        changes = payload.get("changes", [])
        if not isinstance(changes, list):
            msg = "changes must be a list"
            raise ValueError(msg)
        return [change for change in changes if isinstance(change, dict)]


class GitHubDeploymentBackend:
    """Queries the GitHub deployments API for a repo (read-only)."""

    name = "github"

    def __init__(
        self,
        *,
        api_url: str,
        repo: str,
        token: str | None = None,
        timeout_seconds: float = 2.0,
        client: httpx.Client | None = None,
    ) -> None:
        self.api_url = api_url.rstrip("/")
        self.repo = repo
        self.token = token
        self.timeout_seconds = timeout_seconds
        self.client = client

    def fetch_changes(self, service: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
        headers = {"Accept": "application/vnd.github+json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        # The GitHub deployments API has no time-range filter, so the tool filters
        # by window client-side. Fetch a generous page so an incident's deploy is
        # unlikely to fall outside it; deep-history pagination is a follow-up.
        params: dict[str, str | int] = {"environment": service, "per_page": 100}
        path = f"/repos/{self.repo}/deployments"
        if self.client is not None:
            response = self.client.get(
                path, params=params, headers=headers, timeout=self.timeout_seconds
            )
        else:
            with httpx.Client(base_url=self.api_url, timeout=self.timeout_seconds) as client:
                response = client.get(path, params=params, headers=headers)
        response.raise_for_status()
        deployments = response.json()
        if not isinstance(deployments, list):
            msg = "github deployments response must be a list"
            raise ValueError(msg)
        return [_change_from_github(item, service) for item in deployments]


class ArgoCDDeploymentBackend:
    """Queries an Argo CD application's sync history (read-only)."""

    name = "argocd"

    def __init__(
        self,
        *,
        base_url: str,
        token: str | None = None,
        timeout_seconds: float = 2.0,
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout_seconds = timeout_seconds
        self.client = client

    def fetch_changes(self, service: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
        headers = {}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        path = f"/api/v1/applications/{service}"
        if self.client is not None:
            response = self.client.get(path, headers=headers, timeout=self.timeout_seconds)
        else:
            with httpx.Client(base_url=self.base_url, timeout=self.timeout_seconds) as client:
                response = client.get(path, headers=headers)
        response.raise_for_status()
        payload = response.json()
        history = payload.get("status", {}).get("history", []) if isinstance(payload, dict) else []
        changes = [_change_from_argocd(item, service) for item in history]
        # Argo history is oldest-first; the tool expects newest-first.
        return list(reversed(changes))


def _change_from_github(item: dict[str, Any], service: str) -> dict[str, Any]:
    creator = item.get("creator") or {}
    return {
        "service": service,
        "deployed_at": item.get("created_at"),
        "commit_sha": str(item.get("sha", ""))[:7],
        "author": creator.get("login"),
        "summary": item.get("description") or item.get("ref"),
        "files": [],
    }


def _change_from_argocd(item: dict[str, Any], service: str) -> dict[str, Any]:
    return {
        "service": service,
        "deployed_at": item.get("deployedAt"),
        "commit_sha": str(item.get("revision", ""))[:7],
        "author": None,
        "summary": f"Argo CD sync to {str(item.get('revision', ''))[:7]}",
        "files": [],
    }


def build_deployment_backend(settings: Settings) -> DeploymentBackend:
    """Select the deployment backend from settings (default: fixture)."""
    backend = settings.deployment_backend.strip().lower()
    if backend == "fixture":
        return FixtureDeploymentBackend(fixture_path=settings.git_changes_fixture_path)
    if backend == "github":
        if not settings.github_repo:
            msg = "deployment_backend=github requires github_repo"
            raise ValueError(msg)
        token = settings.github_token.get_secret_value() if settings.github_token else None
        return GitHubDeploymentBackend(
            api_url=settings.github_api_url,
            repo=settings.github_repo,
            token=token,
            timeout_seconds=settings.tool_timeout_seconds,
        )
    if backend == "argocd":
        token = settings.argocd_token.get_secret_value() if settings.argocd_token else None
        return ArgoCDDeploymentBackend(
            base_url=settings.argocd_url,
            token=token,
            timeout_seconds=settings.tool_timeout_seconds,
        )
    msg = f"unknown deployment_backend '{settings.deployment_backend}'"
    raise ValueError(msg)
