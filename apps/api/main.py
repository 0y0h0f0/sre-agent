from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request
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
    settings = get_settings()
    app = FastAPI(title="SRE Incident Response Agent", version="0.1.0")

    # CORS (Phase 7.4)
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
    app.middleware("http")(_request_id_middleware)
    app.middleware("http")(create_api_key_middleware())
    app.add_exception_handler(AppError, _app_error_handler)
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
    request_id = request.headers.get("X-Request-Id") or new_id("req_")
    request.state.request_id = request_id
    response = await call_next(request)
    response.headers["X-Request-Id"] = request_id
    return response


async def _app_error_handler(request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, AppError):
        raise exc
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


async def _validation_error_handler(request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, RequestValidationError):
        raise exc
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


app = create_app()
