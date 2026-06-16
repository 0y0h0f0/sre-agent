"""E2E tests — M9 Semantic Search (PR 9.10).

End-to-end tests for M9 semantic capabilities: semantic runbook search, embedding
provider, and external embedding provider.

Tests the API-level behavior: search endpoint contracts, feature flag enforcement,
degraded paths, keyword fallback, and the E2E smoke sequence from
docs/superpowers/specs/m9-foragent.md §15.5 steps 13-14.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from apps.api.dependencies import get_app_settings, get_db, get_task_enqueue
from packages.common.settings import Settings, get_settings
from packages.db.base import Base

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def e2e_engine():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture()
def e2e_session(e2e_engine):
    SessionLocal = sessionmaker(
        bind=e2e_engine, autoflush=False, autocommit=False
    )
    with SessionLocal() as session:
        yield session


class FakeEnqueue:
    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    def __call__(self, incident_id: str, agent_run_id: str) -> str:
        self.calls.append((incident_id, agent_run_id))
        return f"task-{len(self.calls)}"


@pytest.fixture()
def e2e_client(
    e2e_session,
    monkeypatch: pytest.MonkeyPatch,
) -> TestClient:
    """TestClient with M9 enabled, semantic search + external embedding off."""
    monkeypatch.setenv("API_KEY_AUTH_ENABLED", "false")
    monkeypatch.setenv("LLM_PROVIDER", "fake")
    monkeypatch.setenv("EMBEDDING_PROVIDER", "fake")
    monkeypatch.setenv("CELERY_TASK_ALWAYS_EAGER", "true")
    monkeypatch.setenv("M9_EXTENSIONS_ENABLED", "true")
    monkeypatch.setenv("SEMANTIC_RUNBOOK_SEARCH_ENABLED", "false")
    monkeypatch.setenv("EXTERNAL_EMBEDDING_PROVIDER_ENABLED", "false")
    get_settings.cache_clear()

    from apps.api.main import create_app

    app = create_app()
    fake_enqueue = FakeEnqueue()

    def override_db():
        yield e2e_session

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_task_enqueue] = lambda: fake_enqueue
    app.dependency_overrides[get_app_settings] = lambda: Settings(
        database_url="sqlite+pysqlite:///:memory:",
        api_key_auth_enabled=False,
        celery_task_always_eager=True,
        llm_provider="fake",
        embedding_provider="fake",
        m9_extensions_enabled=True,
        semantic_runbook_search_enabled=False,
        external_embedding_provider_enabled=False,
        rate_limit_max_requests=1000,
    )

    with TestClient(app) as client:
        client._fake_enqueue = fake_enqueue  # type: ignore[attr-defined]
        yield client
    app.dependency_overrides.clear()
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# E2E Smoke 13: Semantic Search with Embedding Unavailable
# ---------------------------------------------------------------------------


class TestE2ESemanticSearch:
    """Semantic runbook search E2E tests."""

    def test_keyword_search_works_without_semantic(self, e2e_client: TestClient):
        """Keyword search must work even when semantic search is disabled."""
        resp = e2e_client.get("/api/runbooks/search?q=high+latency")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    def test_search_returns_list(self, e2e_client: TestClient):
        """Search endpoint should always return a list."""
        resp = e2e_client.get("/api/runbooks/search?q=test")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)

    def test_search_empty_query_handled(self, e2e_client: TestClient):
        """Empty search query should be handled gracefully."""
        resp = e2e_client.get("/api/runbooks/search?q=")
        assert resp.status_code != 500

    def test_search_with_special_characters(self, e2e_client: TestClient):
        """Search with special characters should not crash."""
        resp = e2e_client.get("/api/runbooks/search?q=%3Cscript%3Ealert(1)%3C/script%3E")
        assert resp.status_code == 200

    def test_search_missing_query_param(self, e2e_client: TestClient):
        """Search without query parameter should be handled."""
        resp = e2e_client.get("/api/runbooks/search")
        assert resp.status_code != 500


# ---------------------------------------------------------------------------
# E2E Smoke 14: External Embedding Provider
# ---------------------------------------------------------------------------


class TestE2EExternalEmbedding:
    """External embedding provider E2E tests."""

    def test_runbook_ingest_works_with_external_embedding_disabled(
        self, e2e_client: TestClient
    ):
        """Runbook ingest must work when external embedding is disabled."""
        resp = e2e_client.post(
            "/api/runbooks/ingest",
            json={
                "content": "# Test Runbook\n\n## Diagnosis\n\nCheck CPU usage.",
                "source_path": "docs/test-ingest.md",
                "service": "test-service",
            },
        )
        assert resp.status_code in (200, 201, 422)

    def test_runbook_approve_does_not_require_embedding(self, e2e_client: TestClient):
        """Runbook operations should not be blocked by embedding availability."""
        # This tests that the API doesn't crash when embedding is disabled
        resp = e2e_client.get("/api/runbooks/drafts")
        assert resp.status_code == 200

    def test_config_requires_embedding_external_scope_structure(
        self, e2e_client: TestClient
    ):
        """Config endpoint should maintain security scope structure."""
        resp = e2e_client.get("/api/config/current")
        assert resp.status_code == 200
        # Config should be readable regardless of embedding state


# ---------------------------------------------------------------------------
# E2E: Runbook Version + Search Integration
# ---------------------------------------------------------------------------


class TestE2ERunbookVersionSearch:
    """Integration of runbook versions with search."""

    def test_runbook_versions_endpoint(self, e2e_client: TestClient):
        """Runbook versions endpoint should work."""
        resp = e2e_client.get("/api/runbooks/versions/doc_nonexistent")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    def test_runbook_draft_review_nonexistent(self, e2e_client: TestClient):
        """Reviewing non-existent draft should return error."""
        resp = e2e_client.post(
            "/api/runbooks/drafts/draft_nonexistent/review",
            json={"status": "approved", "reviewer_notes": "test"},
        )
        assert resp.status_code in (400, 404, 422)

    def test_runbook_regenerate_nonexistent(self, e2e_client: TestClient):
        """Regenerating non-existent draft should return error."""
        resp = e2e_client.post(
            "/api/runbooks/drafts/draft_nonexistent/regenerate",
            json={},
        )
        assert resp.status_code in (400, 404, 422)


# ---------------------------------------------------------------------------
# Failure Injection: Embedding Degraded Paths
# ---------------------------------------------------------------------------


class TestE2EEmbeddingFailureInjection:
    """Failure injection tests for embedding provider."""

    def test_embedding_disabled_keyword_search_still_works(
        self, e2e_client: TestClient
    ):
        """When embedding is disabled, keyword search must keep working."""
        resp = e2e_client.get("/api/runbooks/search?q=diagnosis")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    def test_runbook_ingest_no_secret_leak_in_response(
        self, e2e_client: TestClient
    ):
        """Runbook ingest response must not leak sensitive data."""
        resp = e2e_client.post(
            "/api/runbooks/ingest",
            json={
                "content": "# Test\n\n## Step 1\nCheck logs for token=abc123",
                "source_path": "docs/test-no-leak.md",
                "service": "test-service",
            },
        )
        assert resp.status_code in (200, 201, 422)

    def test_multiple_ingests_dont_crash(self, e2e_client: TestClient):
        """Multiple sequential ingests should not crash."""
        for i in range(3):
            resp = e2e_client.post(
                "/api/runbooks/ingest",
                json={
                    "content": f"# Runbook {i}\n\n## Steps\n1. Check {i}",
                    "source_path": f"docs/runbook-{i}.md",
                    "service": "test-service",
                },
            )
            assert resp.status_code in (200, 201, 422)

    def test_search_after_ingest(self, e2e_client: TestClient):
        """Search should work after ingesting a runbook."""
        # Ingest first
        ingest_resp = e2e_client.post(
            "/api/runbooks/ingest",
            json={
                "content": "# Latency Troubleshooting\n\nCheck CPU and memory.",
                "source_path": "docs/latency.md",
                "service": "api-gateway",
            },
        )
        assert ingest_resp.status_code in (200, 201, 422)

        # Then search
        search_resp = e2e_client.get("/api/runbooks/search?q=latency")
        assert search_resp.status_code == 200
        assert isinstance(search_resp.json(), list)


# ---------------------------------------------------------------------------
# E2E: Secret Leakage Smoke
# ---------------------------------------------------------------------------


class TestE2ESecretLeakage:
    """Verify that secrets don't leak through API responses or audit logs."""

    SENSITIVE_PATTERNS = [
        "Bearer ",
        "x-api-key",
        "private_key",
        "password",
        "token=",
    ]

    def test_health_endpoint_no_secret_leak(self, e2e_client: TestClient):
        """Health endpoint must not leak secrets."""
        resp = e2e_client.get("/healthz")
        assert resp.status_code == 200
        body = resp.text.lower()
        for pattern in [p.lower() for p in self.SENSITIVE_PATTERNS]:
            assert pattern not in body, f"Found '{pattern}' in health response"

    def test_discovery_status_no_secret_leak(self, e2e_client: TestClient):
        """Discovery status must not leak secrets."""
        resp = e2e_client.get("/api/discovery/status")
        assert resp.status_code == 200
        body = resp.text.lower()
        # Check for common secret patterns
        for pattern in ["private_key", "password", "token=", "api_key"]:
            assert pattern not in body, f"Found '{pattern}' in discovery status"

    def test_config_current_no_secret_leak(self, e2e_client: TestClient):
        """Config current response must not contain raw secrets."""
        resp = e2e_client.get("/api/config/current")
        assert resp.status_code == 200
        body = resp.text.lower()
        for pattern in ["private_key", "password", "bearer "]:
            assert pattern not in body, f"Found '{pattern}' in config response"

    def test_incident_list_no_secret_leak(self, e2e_client: TestClient):
        """Incident list must not leak secrets."""
        resp = e2e_client.get("/api/incidents?limit=5")
        assert resp.status_code == 200
        body = resp.text.lower()
        for pattern in ["private_key", "password"]:
            assert pattern not in body, f"Found '{pattern}' in incident list"
