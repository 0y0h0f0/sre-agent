from __future__ import annotations


def _write_runbooks(base) -> None:
    base.mkdir()
    (base / "high.md").write_text(
        """---
service: checkout
incident_type: high_5xx
severity: P1
owner: payment-team
updated_at: 2026-05-31
---
# High 5xx Triage

## Detection
Checkout-api high 5xx after deploy requires metrics, logs, traces, and git evidence.

## Rollback Checks
Rollback checks require previous image and backward compatible migrations.
""",
        encoding="utf-8",
    )
    (base / "cache.md").write_text(
        """---
service: checkout
incident_type: cache_avalanche
severity: P1
owner: payment-team
updated_at: 2026-05-31
---
# Redis Cache Avalanche

## Detection
Redis cache hit rate drops while database pressure increases.
""",
        encoding="utf-8",
    )


def test_runbook_ingest_and_search_api(client, tmp_path) -> None:
    base = tmp_path / "runbooks"
    _write_runbooks(base)

    ingest = client.post("/api/runbooks/ingest", json={"path": str(base)})
    assert ingest.status_code == 200
    body = ingest.json()
    assert body["files_scanned"] == 2
    assert body["chunks_created"] > 0
    assert body["chunks_total"] == body["chunks_created"]

    repeated = client.post("/api/runbooks/ingest", json={"path": str(base)})
    assert repeated.status_code == 200
    assert repeated.json()["chunks_created"] == 0
    assert repeated.json()["chunks_skipped"] == body["chunks_created"]

    search = client.get(
        "/api/runbooks/search",
        params={
            "q": "high 5xx after deploy rollback",
            "service": "checkout",
            "incident_type": "high_5xx",
            "top_k": 2,
        },
    )
    assert search.status_code == 200
    results = search.json()
    assert results
    assert results[0]["chunk_id"].startswith("chk_")
    assert results[0]["source_path"].endswith("high.md")
    assert results[0]["metadata"]["incident_type"] == "high_5xx"


def test_runbook_ingest_missing_path_returns_standard_error(client, tmp_path) -> None:
    response = client.post(
        "/api/runbooks/ingest",
        json={"path": str(tmp_path / "missing")},
        headers={"X-Request-Id": "req-runbook"},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert response.json()["error"]["request_id"] == "req-runbook"
