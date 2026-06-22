"""Integration coverage for M9 PR 9.4 web-search draft context."""

from __future__ import annotations

from apps.api.routers.runbooks import web_search
from apps.api.schemas.runbooks import WebSearchRequest
from packages.common.settings import Settings


def test_web_search_endpoint_returns_draft_traceability() -> None:
    settings = Settings(
        api_key_auth_enabled=False,
        redis_url="memory://web-search-endpoint",
        m9_extensions_enabled=True,
        runbook_web_search_enabled=True,
        runbook_web_search_provider="fake",
    )
    response = web_search(
        WebSearchRequest(
            query="service=checkout password=s3cret high 5xx runbook",
            purpose="draft_enrichment",
        ),
        settings=settings,
        _scope=None,
    )

    payload = response.model_dump()
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
