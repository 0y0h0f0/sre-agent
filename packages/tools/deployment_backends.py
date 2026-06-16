"""Deployment/change backends for the GitChangeTool (roadmap Phase 2.1).

The fixture backend preserves MVP behaviour. GitHub and Argo CD backends query
real deployment history and are only contacted when ``deployment_backend`` is
set away from ``fixture``. All backends return change dicts in one shape:

    {service, deployed_at, commit_sha, author, summary, files}
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import httpx

from packages.common.settings import Settings

_MAX_COMMIT_DETAIL_LOOKUPS = 30


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

        if self.client is not None:
            return self._fetch_with_client(self.client, service, start, end, headers)
        with httpx.Client(base_url=self.api_url, timeout=self.timeout_seconds) as client:
            return self._fetch_with_client(client, service, start, end, headers)

    def _fetch_with_client(
        self,
        client: httpx.Client,
        service: str,
        start: datetime,
        end: datetime,
        headers: dict[str, str],
    ) -> list[dict[str, Any]]:
        # Prefer official GitHub deployment records when the repo publishes them.
        deployments = self._get_json(
            client,
            f"/repos/{self.repo}/deployments",
            params={"environment": service, "per_page": 100},
            headers=headers,
        )
        if not isinstance(deployments, list):
            msg = "github deployments response must be a list"
            raise ValueError(msg)
        if deployments:
            return [_change_from_github(item, service) for item in deployments]

        # Many repos keep deployable service code on GitHub but do not create
        # GitHub Deployment objects. Fall back to commit history for the incident
        # window, keeping only commits that touch paths related to the service.
        commits = self._get_json(
            client,
            f"/repos/{self.repo}/commits",
            params={
                "since": _github_timestamp(start),
                "until": _github_timestamp(end),
                "per_page": _MAX_COMMIT_DETAIL_LOOKUPS,
            },
            headers=headers,
        )
        if not isinstance(commits, list):
            msg = "github commits response must be a list"
            raise ValueError(msg)

        changes: list[dict[str, Any]] = []
        for item in commits[:_MAX_COMMIT_DETAIL_LOOKUPS]:
            if not isinstance(item, dict):
                continue
            sha = item.get("sha")
            if not sha:
                continue
            detail = self._get_json(
                client, f"/repos/{self.repo}/commits/{sha}", params={}, headers=headers
            )
            if not isinstance(detail, dict):
                continue
            files = _commit_files(detail)
            if not _commit_matches_service(files, service):
                continue
            changes.append(_change_from_github_commit(detail, item, service, files))
        return changes

    def _get_json(
        self,
        client: httpx.Client,
        path: str,
        *,
        params: dict[str, str | int],
        headers: dict[str, str],
    ) -> Any:
        response = client.get(
            path, params=params, headers=headers, timeout=self.timeout_seconds
        )
        response.raise_for_status()
        return response.json()


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


def _change_from_github_commit(
    detail: dict[str, Any],
    item: dict[str, Any],
    service: str,
    files: list[str],
) -> dict[str, Any]:
    detail_commit = detail.get("commit")
    commit = detail_commit if isinstance(detail_commit, dict) else {}
    item_commit = item.get("commit")
    fallback_commit = item_commit if isinstance(item_commit, dict) else {}
    commit_payload: dict[str, Any] = commit or fallback_commit
    committer_payload = commit_payload.get("committer") or commit_payload.get("author")
    committer = committer_payload if isinstance(committer_payload, dict) else {}
    author_payload = detail.get("author") or item.get("author")
    author = author_payload if isinstance(author_payload, dict) else {}
    commit_author_payload = commit_payload.get("author")
    commit_author = commit_author_payload if isinstance(commit_author_payload, dict) else {}
    sha = str(detail.get("sha") or item.get("sha") or "")
    message = str(commit_payload.get("message") or sha).splitlines()[0]
    return {
        "service": service,
        "deployed_at": committer.get("date"),
        "commit_sha": sha[:7],
        "author": author.get("login") or commit_author.get("name"),
        "summary": message,
        "files": files[:20],
    }


def _commit_files(detail: dict[str, Any]) -> list[str]:
    files = detail.get("files")
    if not isinstance(files, list):
        return []
    names: list[str] = []
    for item in files:
        if isinstance(item, dict) and isinstance(item.get("filename"), str):
            names.append(item["filename"])
    return names


def _commit_matches_service(files: list[str], service: str) -> bool:
    if not files:
        return False
    normalized = service.strip().lower()
    candidates = {normalized, normalized.replace("-", "_")}
    for part in re.split(r"[-_]+", normalized):
        if part and part not in {"api", "service"}:
            candidates.add(part)

    for filename in files:
        lowered = filename.lower()
        if normalized in lowered or normalized.replace("-", "_") in lowered:
            return True
        segments = {part for part in re.split(r"[/._-]+", lowered) if part}
        if candidates.intersection(segments):
            return True
    return False


def _github_timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


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
