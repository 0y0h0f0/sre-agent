"""API key authentication middleware.

Only enforces auth when ``api_key_auth_enabled`` is True (default).
Open paths (health, docs, metrics) skip auth unconditionally.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Awaitable, Callable

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from apps.api.services.api_key_service import ApiKeyService
from packages.common.ids import new_id
from packages.common.settings import get_settings
from packages.db.session import SessionLocal

logger = logging.getLogger(__name__)
RequestCallNext = Callable[[Request], Awaitable[Response]]


def _get_open_paths() -> set[str]:
    raw = get_settings().api_key_open_paths
    paths = set()
    for p in raw.split(","):
        p = p.strip()
        if not p:
            continue
        if not p.startswith("/"):
            logger.warning("api_key_open_paths entry %r missing leading slash — ignored", p)
            continue
        paths.add(p.rstrip("/"))
    return paths


def _normalize_path(path: str) -> str:
    """Normalize a request path, resolving ``..`` segments."""
    normalized = os.path.normpath(path)
    # normpath on "/healthz/../api/incidents" -> "/api/incidents" on Linux
    return normalized


def _is_open_path(request_path: str) -> bool:
    """Check whether *request_path* matches any configured open path.

    Uses boundary-aware matching: a path matches only if it equals the
    open path exactly or starts with ``<open_path>/``.
    """
    normalized = _normalize_path(request_path)
    for open_path in _get_open_paths():
        if normalized == open_path:
            return True
        if normalized.startswith(open_path + "/"):
            return True
    return False


def _check_initial_key(raw_key: str) -> bool:
    """Return True when *raw_key* matches the bootstrap initial API key."""
    settings = get_settings()
    seed = settings.api_key_initial_seed
    if not seed:
        return False
    import hmac

    return hmac.compare_digest(raw_key, seed.get_secret_value())


def create_api_key_middleware() -> Callable[[Request, RequestCallNext], Awaitable[Response]]:
    async def middleware(request: Request, call_next: RequestCallNext) -> Response:
        settings = get_settings()

        if not settings.api_key_auth_enabled:
            return await call_next(request)

        if _is_open_path(request.url.path):
            return await call_next(request)

        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return _unauthorized(request)

        raw_key = auth_header[len("Bearer "):].strip()
        if not raw_key:
            return _unauthorized(request)

        db: Session = SessionLocal()
        try:
            service = ApiKeyService(db)
            # Bootstrap: the initial seed is valid only before any API key has
            # ever been created. After first use it must not remain an admin
            # credential.
            if _check_initial_key(raw_key):
                if service.has_any_keys():
                    logger.warning("bootstrap API key seed rejected after key store initialization")
                    return _unauthorized(request)
                request.state.api_key = {
                    "key_id": "apik_initial",
                    "description": "initial-seed",
                    "created_by": "system",
                    "scopes": ["api_key:admin"],
                    "roles": ["bootstrap"],
                    "is_bootstrap": True,
                }
                return await call_next(request)

            identity = service.verify(raw_key)
            if identity is None:
                return _unauthorized(request)

            request.state.api_key = identity
            response = await call_next(request)

            # Best-effort touch of last_used_at after response
            try:
                service.touch_used(identity["key_id"])
                db.commit()
            except Exception:
                logger.warning(
                    "failed to update last_used_at for key %s",
                    identity["key_id"], exc_info=True,
                )

            return response
        finally:
            db.close()

    return middleware


def _unauthorized(request: Request | None = None) -> JSONResponse:
    request_id = ""
    if request is not None:
        request_id = getattr(request.state, "request_id", None) or new_id("req_")
    else:
        request_id = new_id("req_")
    return JSONResponse(
        status_code=401,
        content={
            "error": {
                "code": "UNAUTHORIZED",
                "message": "unauthorized",
                "request_id": request_id,
                "details": {},
            }
        },
        headers={"X-Request-Id": request_id} if request_id else {},
    )
