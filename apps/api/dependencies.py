from __future__ import annotations

from collections.abc import Callable, Generator
from typing import Any

from fastapi import Request
from sqlalchemy.orm import Session

from packages.common.settings import Settings, get_settings
from packages.db.session import get_session

TaskEnqueue = Callable[[str, str], str]
NotificationTaskEnqueue = Callable[[str, dict[str, Any]], str]


def get_db() -> Generator[Session, None, None]:
    yield from get_session()


def get_app_settings() -> Settings:
    return get_settings()


ResumeTaskEnqueue = Callable[[str, str], str]


def get_task_enqueue() -> TaskEnqueue:
    from apps.worker.tasks import enqueue_diagnosis_task

    return enqueue_diagnosis_task


def get_resume_task_enqueue() -> ResumeTaskEnqueue:
    from apps.worker.tasks import enqueue_resume_task

    return enqueue_resume_task


def get_notification_task_enqueue() -> NotificationTaskEnqueue:
    from apps.worker.tasks import enqueue_email_notification_task

    return enqueue_email_notification_task


def get_current_api_key(request: Request) -> dict[str, str]:
    """Return the API key identity from the request state.

    Requires the api_key middleware to be active. Returns an empty dict
    when auth is disabled to allow dependency injection to work in tests.
    """
    return getattr(request.state, "api_key", {})


class ScopeRequirement:
    """FastAPI dependency factory requiring at least one of the given scopes.

    Usage::

        require_config_write = require_scope("config:write")

        @router.post("/config/publish", dependencies=[require_config_write])
    """

    def __init__(self, *required_scopes: str) -> None:
        self._required = set(required_scopes)

    def __call__(self, request: Request) -> None:
        from fastapi import HTTPException

        from packages.common.settings import get_settings

        # When auth is disabled (local/CI), skip scope checks entirely.
        settings = get_settings()
        if not settings.api_key_auth_enabled:
            return

        api_key: dict[str, object] = getattr(request.state, "api_key", {})
        if not api_key:
            raise HTTPException(status_code=401, detail="Authentication required")

        raw_scopes: Any = api_key.get("scopes", [])
        scopes: list[str] = list(raw_scopes) if isinstance(raw_scopes, list) else []
        if not self._required.intersection(scopes):
            raise HTTPException(
                status_code=403,
                detail=(
                    "Missing required scope(s): "
                    f"{', '.join(sorted(self._required))}"
                ),
            )


def require_scope(*scopes: str) -> ScopeRequirement:
    """FastAPI dependency — requires at least one of *scopes."""
    return ScopeRequirement(*scopes)


def require_any_scope(*scopes: str) -> ScopeRequirement:
    """Alias for require_scope."""
    return ScopeRequirement(*scopes)


# ---------------------------------------------------------------------------
# M9 Permission Scopes (PR 9.1)
# ---------------------------------------------------------------------------
# These scope constants extend the existing ApiKey scopes system with M9-specific
# permissions. They are used with require_scope() in M9 router endpoints.
#
# Usage::
#     require_runbook_llm = require_scope("runbook:llm_generate")
#     require_embedding_external = require_scope("config:write", "embedding:external")

# Runbook read access (also usable outside M9).
SCOPE_RUNBOOK_READ = "runbook:read"
# Web search for runbook enrichment (PR 9.4).
SCOPE_RUNBOOK_WEB_SEARCH = "runbook:web_search"
# LLM-based runbook draft generation (PR 9.2).
SCOPE_RUNBOOK_LLM_GENERATE = "runbook:llm_generate"
# LLM incident vs runbook diff analysis (PR 9.3).
SCOPE_INCIDENT_LLM_DIFF = "incident:llm_diff"
# Generic LLM invocation (for external cloud LLM).
SCOPE_LLM_INVOKE = "llm:invoke"
# External AI provider access (broader than llm:invoke).
SCOPE_AI_EXTERNAL = "ai:external"
# External embedding provider configuration (PR 9.9).
SCOPE_EMBEDDING_EXTERNAL = "embedding:external"
