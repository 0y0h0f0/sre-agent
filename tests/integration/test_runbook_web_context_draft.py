"""Integration coverage for M9 PR 9.4 web-search draft context."""

from __future__ import annotations

from fastapi.testclient import TestClient

from apps.api.dependencies import get_app_settings
from apps.api.main import create_app
from packages.common.settings import Settings


def test_web_search_endpoint_returns_draft_traceability() -> None:
    app = create_app()
    settings = Settings(
        api_key_auth_enabled=False,
        m9_extensions_enabled=True,
        runbook_web_search_enabled=True,
        runbook_web_search_provider="fake",
    )
    app.dependency_overrides[get_app_settings] = lambda: settings

    with TestClient(app) as client:
        response = client.post(
            "/api/runbooks/web-search",
            json={
                "query": "service=checkout password=s3cret high 5xx runbook",
                "purpose": "draft_enrichment",
            },
        )

    app.dependency_overrides.clear()

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["purpose"] == "draft_enrichment"
    assert "checkout" not in payload["query_redacted"]
    assert "s3cret" not in payload["query_redacted"]
    assert payload["results"]
    first = payload["results"][0]
    assert first["original_url"]
    assert first["final_url"]
    assert first["retrieved_at"]
    assert first["content_hash"]
    assert first["redaction_version"]
