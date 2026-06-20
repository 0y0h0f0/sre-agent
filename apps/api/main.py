from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse, Response

from apps.api.middleware.auth import create_api_key_middleware
from apps.api.routers import (
    actions,
    agent_runs,
    alerts,
    api_keys,
    approval_groups,
    approvals,
    comments,
    config,
    discovery,
    evals,
    health,
    incidents,
    reports,
    runbooks,
)
from apps.api.ws.router import router as ws_router
from packages.common.errors import AppError
from packages.common.ids import new_id
from packages.common.settings import get_settings

RequestCallNext = Callable[[Request], Awaitable[Response]]


def create_app() -> FastAPI:
    # Resolve settings once during app construction. Tests can still override
    # dependency-provided settings without mutating this application shell.
    settings = get_settings()
    app = FastAPI(title="SRE Incident Response Agent", version="0.1.0")

    # CORS (Phase 7.4)
    # Empty CORS config means "do not install CORS middleware"; this keeps local
    # and production defaults explicit instead of silently allowing every origin.
    origins = [o.strip() for o in settings.cors_allow_origins.split(",") if o.strip()]
    if origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_credentials="*" not in origins,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    app.add_middleware(GZipMiddleware, minimum_size=1000)
    # Request IDs are attached before route handlers and echoed on every response
    # so API errors, worker enqueue logs, and frontend failures can be correlated.
    app.middleware("http")(_request_id_middleware)
    # API key middleware owns authentication. Scope dependencies in routers only
    # inspect the identity it places on request.state.
    app.middleware("http")(create_api_key_middleware())
    app.add_exception_handler(AppError, _app_error_handler)
    app.add_exception_handler(HTTPException, _http_exception_handler)
    app.add_exception_handler(RequestValidationError, _validation_error_handler)

    app.include_router(health.router)
    app.include_router(alerts.router)
    app.include_router(incidents.router)
    app.include_router(agent_runs.router)
    app.include_router(runbooks.router)
    app.include_router(reports.router)
    app.include_router(approvals.router)
    app.include_router(actions.router)
    app.include_router(comments.router)
    app.include_router(approval_groups.router)
    app.include_router(api_keys.router)
    app.include_router(config.router)
    app.include_router(discovery.router)
    app.include_router(evals.router)
    app.include_router(ws_router)
    return app


async def _request_id_middleware(request: Request, call_next: RequestCallNext) -> Response:
    # Honor caller-provided IDs for idempotency/debugging, otherwise generate a
    # public request ID with the same prefix convention as persisted resources.
    request_id = request.headers.get("X-Request-Id") or new_id("req_")
    request.state.request_id = request_id
    response = await call_next(request)
    response.headers["X-Request-Id"] = request_id
    return response


async def _app_error_handler(request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, AppError):
        raise exc
    # AppError already carries the public code/status/details; this handler only
    # wraps it in the standard API envelope and adds the request ID.
    request_id = getattr(request.state, "request_id", new_id("req_"))
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": {
                "code": exc.code,
                "message": exc.message,
                "request_id": request_id,
                "details": exc.details,
            }
        },
        headers={"X-Request-Id": request_id},
    )


async def _http_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, HTTPException):
        raise exc
    request_id = getattr(request.state, "request_id", new_id("req_"))
    detail = jsonable_encoder(exc.detail)
    if isinstance(detail, str):
        message = detail
        details: dict[str, object] = {}
    else:
        message = "request failed"
        details = {"detail": detail}

    # Preserve headers from FastAPI/security exceptions, then add X-Request-Id so
    # auth and scope failures still match the project error contract.
    headers = dict(exc.headers or {})
    headers["X-Request-Id"] = request_id
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": {
                "code": _http_error_code(exc.status_code),
                "message": message,
                "request_id": request_id,
                "details": details,
            }
        },
        headers=headers,
    )


async def _validation_error_handler(request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, RequestValidationError):
        raise exc
    # FastAPI validation errors expose rich field locations; keep them in details
    # while using a stable top-level code for clients.
    request_id = getattr(request.state, "request_id", new_id("req_"))
    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "code": "VALIDATION_ERROR",
                "message": "request validation failed",
                "request_id": request_id,
                "details": {"errors": jsonable_encoder(exc.errors())},
            }
        },
        headers={"X-Request-Id": request_id},
    )


def _http_error_code(status_code: int) -> str:
    # Map framework-raised HTTPException statuses onto the same public error
    # vocabulary used by AppError subclasses.
    if status_code == 400:
        return "VALIDATION_ERROR"
    if status_code == 401:
        return "UNAUTHORIZED"
    if status_code == 403:
        return "FORBIDDEN"
    if status_code == 404:
        return "NOT_FOUND"
    if status_code == 409:
        return "CONFLICT"
    if status_code == 422:
        return "VALIDATION_ERROR"
    return "HTTP_ERROR"


app = create_app()
