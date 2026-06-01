from __future__ import annotations


def test_healthz(client) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readyz_uses_dependency_checks(client) -> None:
    response = client.get("/readyz")
    assert response.status_code == 200
    assert response.json()["dependencies"] == {
        "postgres": "ok",
        "redis": "ok",
        "celery_broker": "ok",
    }


def test_metrics_endpoint_exposes_prometheus_text(client) -> None:
    response = client.get("/metrics")
    assert response.status_code == 200
    assert "text/plain" in response.headers["content-type"]
    assert b"python_info" in response.content
