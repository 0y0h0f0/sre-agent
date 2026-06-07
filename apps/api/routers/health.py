from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse, Response

try:
    from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
    _PROMETHEUS_AVAILABLE = True
except ImportError:
    _PROMETHEUS_AVAILABLE = False
from redis import Redis
from sqlalchemy import text
from sqlalchemy.orm import Session

from apps.api.dependencies import get_app_settings, get_db
from packages.common.settings import Settings

router = APIRouter(tags=["health"])


@router.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/metrics")
def metrics() -> Response:
    if not _PROMETHEUS_AVAILABLE:
        return Response(
            content=b"prometheus_client not installed",
            status_code=503,
            media_type="text/plain",
        )
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@router.get("/readyz")
def readyz(
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_app_settings),
) -> JSONResponse:
    dependencies: dict[str, str] = {}
    dependencies["postgres"] = _check_database(db)
    dependencies["redis"] = _check_redis(settings.redis_url)
    dependencies["celery_broker"] = _check_redis(settings.celery_broker_url)
    ready = all(status == "ok" for status in dependencies.values())
    return JSONResponse(
        status_code=200 if ready else 503,
        content={"status": "ready" if ready else "not_ready", "dependencies": dependencies},
    )


def _check_database(db: Session) -> str:
    try:
        db.execute(text("SELECT 1"))
    except Exception:
        return "unavailable"
    return "ok"


def _check_redis(url: str) -> str:
    if url.startswith("memory://"):
        return "ok"
    try:
        Redis.from_url(url, socket_connect_timeout=0.2, socket_timeout=0.2).ping()
    except Exception:
        return "unavailable"
    return "ok"
