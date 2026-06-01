from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from starlette.responses import Response

SERVICE = "checkout"

app = FastAPI(title="SRE Demo Service")
logger = logging.getLogger("demo_service")
logging.basicConfig(level=logging.INFO, format="%(message)s")

http_requests = Counter(
    "http_requests_total",
    "Demo HTTP requests",
    ["service", "status"],
)
request_duration = Histogram(
    "http_request_duration_seconds",
    "Demo request duration",
    ["service"],
)
db_connections = Gauge(
    "db_connections_active",
    "Active database connections",
    ["service"],
)
redis_cache_hit_rate = Gauge(
    "redis_cache_hit_rate",
    "Redis cache hit rate",
    ["service"],
)
process_memory = Gauge(
    "process_resident_memory_bytes",
    "Process resident memory",
    ["service"],
)
pod_restarts = Counter(
    "pod_restart_total",
    "Mock pod restart count",
    ["service", "pod"],
)

current_fault: str | None = None


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/metrics")
def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.post("/faults/db-connection-exhaustion")
def db_connection_exhaustion() -> dict[str, Any]:
    _set_fault("db-connection-exhaustion")
    db_connections.labels(service=SERVICE).set(96)
    http_requests.labels(service=SERVICE, status="500").inc(18)
    request_duration.labels(service=SERVICE).observe(1.8)
    _log(
        "error",
        "database_pool_exhausted",
        "database connection pool exhausted: active=96 max=100",
    )
    return _fault_response("db-connection-exhaustion")


@app.post("/faults/high-5xx-after-deploy")
def high_5xx_after_deploy() -> dict[str, Any]:
    _set_fault("high-5xx-after-deploy")
    http_requests.labels(service=SERVICE, status="500").inc(42)
    http_requests.labels(service=SERVICE, status="200").inc(100)
    request_duration.labels(service=SERVICE).observe(0.95)
    _log("error", "http_5xx_after_deploy", "5xx spike after deploy commit a1b2c3d")
    return _fault_response("high-5xx-after-deploy")


@app.post("/faults/cache-avalanche")
def cache_avalanche() -> dict[str, Any]:
    _set_fault("cache-avalanche")
    redis_cache_hit_rate.labels(service=SERVICE).set(0.38)
    db_connections.labels(service=SERVICE).set(82)
    http_requests.labels(service=SERVICE, status="500").inc(8)
    request_duration.labels(service=SERVICE).observe(1.2)
    _log("warn", "redis_cache_avalanche", "redis cache hit rate dropped below 40 percent")
    return _fault_response("cache-avalanche")


@app.post("/faults/pod-restart-loop")
def pod_restart_loop() -> dict[str, Any]:
    _set_fault("pod-restart-loop")
    process_memory.labels(service=SERVICE).set(850 * 1024 * 1024)
    pod_restarts.labels(service=SERVICE, pod="checkout-api-7d8f").inc(4)
    http_requests.labels(service=SERVICE, status="500").inc(12)
    _log("error", "kubernetes_pod_restart", "pod checkout-api-7d8f restarted after OOMKilled")
    return _fault_response("pod-restart-loop")


@app.post("/faults/clear")
def clear_faults() -> dict[str, str | None]:
    global current_fault
    current_fault = None
    db_connections.labels(service=SERVICE).set(18)
    redis_cache_hit_rate.labels(service=SERVICE).set(0.96)
    process_memory.labels(service=SERVICE).set(256 * 1024 * 1024)
    _log("info", "faults_cleared", "demo faults cleared")
    return {"status": "cleared", "fault": current_fault}


def _set_fault(name: str) -> None:
    global current_fault
    current_fault = name


def _fault_response(name: str) -> dict[str, Any]:
    return {"status": "injected", "fault": name, "service": SERVICE}


def _log(level: str, event: str, message: str) -> None:
    logger.info(
        json.dumps(
            {
                "timestamp": datetime.now(UTC).isoformat(),
                "service": SERVICE,
                "level": level,
                "event": event,
                "message": message,
                "fault": current_fault,
            },
            sort_keys=True,
        )
    )
