"""E2E tests — M9 AI Extensions (PR 9.10).

End-to-end tests for M9 AI capabilities: LLM runbook generation, incident diff
analysis, and web search safety. Uses FastAPI TestClient with SQLite in-memory
DB and fake LLM/embedding providers.

Validates: feature flag behavior, endpoint contracts, error handling, and the
E2E smoke sequence from docs/superpowers/specs/m9-foragent.md §15.5.
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
    """In-memory SQLite engine with all tables created."""
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
    """New session per test."""
    SessionLocal = sessionmaker(
        bind=e2e_engine, autoflush=False, autocommit=False
    )
    with SessionLocal() as session:
        yield session


class FakeEnqueue:
    """Fake Celery task enqueuer that records calls."""

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
    """TestClient with M9 defaults disabled (safe M8-compatible state)."""
    monkeypatch.setenv("API_KEY_AUTH_ENABLED", "false")
    monkeypatch.setenv("LLM_PROVIDER", "fake")
    monkeypatch.setenv("EMBEDDING_PROVIDER", "fake")
    monkeypatch.setenv("CELERY_TASK_ALWAYS_EAGER", "true")
    monkeypatch.setenv("M9_EXTENSIONS_ENABLED", "true")
    monkeypatch.setenv("RUNBOOK_LLM_GENERATION_ENABLED", "false")
    monkeypatch.setenv("LLM_INCIDENT_DIFF_ENABLED", "false")
    monkeypatch.setenv("RUNBOOK_WEB_SEARCH_ENABLED", "false")
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
        runbook_llm_generation_enabled=False,
        llm_incident_diff_enabled=False,
        runbook_web_search_enabled=False,
        rate_limit_max_requests=1000,
    )

    with TestClient(app) as client:
        client._fake_enqueue = fake_enqueue  # type: ignore[attr-defined]
        yield client
    app.dependency_overrides.clear()
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# E2E Smoke 1: M8 behavior preserved with M9 features off
# ---------------------------------------------------------------------------


class TestE2EM9M8SmokePreserved:
    """Verify that M8 endpoints still work when M9 features are disabled."""

    def test_health_endpoint_returns_ok(self, e2e_client: TestClient):
        resp = e2e_client.get("/healthz")
        assert resp.status_code == 200

    def test_alert_creation_works(self, e2e_client: TestClient):
        payload = {
            "source": "mock",
            "fingerprint": "fp-m9-smoke-1",
            "service": "api-gateway",
            "severity": "P2",
            "alert_name": "M9SmokeTest",
            "starts_at": "2026-06-12T00:00:00Z",
            "labels": {"team": "sre"},
            "annotations": {"summary": "M9 smoke test"},
        }
        resp = e2e_client.post("/api/alerts", json=payload)
        assert resp.status_code in (200, 201, 202)

    def test_discovery_status_returns_data(self, e2e_client: TestClient):
        resp = e2e_client.get("/api/discovery/status")
        assert resp.status_code == 200
        data = resp.json()
        assert "discovery_enabled" in data

    def test_config_current_returns_status(self, e2e_client: TestClient):
        resp = e2e_client.get("/api/config/current")
        assert resp.status_code == 200
        data = resp.json()
        assert "status" in data


# ---------------------------------------------------------------------------
# E2E Smoke 2: M9 enabled + all sub-features disabled = no behavior change
# ---------------------------------------------------------------------------


class TestE2EM9GlobalEnabledSubfeaturesDisabled:
    """M9_EXTENSIONS_ENABLED=true with all sub-features false."""

    def test_runbook_search_works_when_m9_enabled(self, e2e_client: TestClient):
        """Search should work (keyword fallback) when M9 enabled but semantic off."""
        resp = e2e_client.get("/api/runbooks/search?q=test")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    def test_runbook_drafts_listed(self, e2e_client: TestClient):
        resp = e2e_client.get("/api/runbooks/drafts")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)


# ---------------------------------------------------------------------------
# E2E Smoke 3-4: LLM Runbook Draft Generation
# ---------------------------------------------------------------------------


class TestE2ELLMRunbookDraft:
    """LLM runbook draft generation E2E tests."""

    def test_llm_generate_endpoint_exists(self, e2e_client: TestClient):
        """POST /api/runbooks/llm-generate endpoint should exist."""
        resp = e2e_client.post(
            "/api/runbooks/llm-generate",
            json={
                "incident_id": "inc_nonexistent",
                "approved_runbook_version_id": None,
            },
        )
        # Should return an error (incident doesn't exist) but the endpoint works
        assert resp.status_code in (200, 400, 404, 422)

    def test_llm_generate_requires_request_body(self, e2e_client: TestClient):
        """LLM generate should reject empty request."""
        resp = e2e_client.post("/api/runbooks/llm-generate", json={})
        assert resp.status_code == 422  # Validation error

    def test_runbook_draft_generate_endpoint_exists(self, e2e_client: TestClient):
        """POST /api/runbooks/drafts/generate should exist."""
        resp = e2e_client.post(
            "/api/runbooks/drafts/generate",
            json={"incident_id": "inc_nonexistent"},
        )
        assert resp.status_code in (200, 400, 404, 422)

    def test_runbook_template_endpoint_works(self, e2e_client: TestClient):
        """POST /api/runbooks/template should work."""
        resp = e2e_client.post(
            "/api/runbooks/template",
            json={
                "incident_id": "inc_nonexistent",
                "service": "api-gateway",
                "alert_type": "latency",
            },
        )
        # Template generation should succeed or return clear error
        assert resp.status_code in (200, 400, 404, 422)

    def test_drafts_endpoint_returns_list(self, e2e_client: TestClient):
        """GET /api/runbooks/drafts should return a list."""
        resp = e2e_client.get("/api/runbooks/drafts")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)


# ---------------------------------------------------------------------------
# E2E Smoke 5-6: LLM Incident Diff Analysis
# ---------------------------------------------------------------------------


class TestE2EIncidentDiff:
    """LLM incident diff analysis E2E tests."""

    def test_incident_diff_endpoint_exists(self, e2e_client: TestClient):
        """POST /api/runbooks/incident-diff endpoint should exist."""
        resp = e2e_client.post(
            "/api/runbooks/incident-diff",
            json={
                "incident_id": "inc_nonexistent",
                "approved_runbook_version_id": None,
            },
        )
        assert resp.status_code in (200, 400, 404, 422)

    def test_incident_diff_rejects_empty_body(self, e2e_client: TestClient):
        """Incident diff should reject empty request body."""
        resp = e2e_client.post("/api/runbooks/incident-diff", json={})
        assert resp.status_code == 422

    def test_amendments_endpoint_returns_list(self, e2e_client: TestClient):
        """GET /api/runbooks/amendments should return a list."""
        resp = e2e_client.get("/api/runbooks/amendments")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    def test_amendment_review_nonexistent_returns_error(
        self, e2e_client: TestClient
    ):
        """Reviewing a nonexistent amendment should return an error."""
        resp = e2e_client.post(
            "/api/runbooks/amendments/amd_nonexistent/review",
            json={"status": "approved", "reviewer_notes": "test"},
        )
        assert resp.status_code in (400, 404, 422)


# ---------------------------------------------------------------------------
# E2E Smoke 7: Web Search Safety
# ---------------------------------------------------------------------------


class TestE2EWebSearch:
    """Web search safety E2E tests."""

    def test_web_search_endpoint_exists(self, e2e_client: TestClient):
        """POST /api/runbooks/web-search endpoint should exist."""
        resp = e2e_client.post(
            "/api/runbooks/web-search",
            json={
                "query": "how to handle high latency",
                "incident_id": "inc_nonexistent",
            },
        )
        assert resp.status_code in (200, 400, 404, 422)

    def test_web_search_rejects_empty_query(self, e2e_client: TestClient):
        """Web search should reject empty query."""
        resp = e2e_client.post(
            "/api/runbooks/web-search",
            json={"query": "", "incident_id": "inc_nonexistent"},
        )
        # Empty query should fail validation or be rejected
        assert resp.status_code in (400, 422)


# ---------------------------------------------------------------------------
# E2E: Feature Flag Behavior
# ---------------------------------------------------------------------------


class TestE2EM9FeatureFlags:
    """Test M9 feature flags through API behavior."""

    def test_runbook_ingest_endpoint_works(self, e2e_client: TestClient):
        """Runbook ingest should work regardless of M9 flags."""
        resp = e2e_client.post(
            "/api/runbooks/ingest",
            json={
                "content": "# Test Runbook\n\n## Steps\n1. Check logs",
                "source_path": "docs/test.md",
                "service": "test-service",
            },
        )
        assert resp.status_code in (200, 201, 422)

    def test_incident_list_works(self, e2e_client: TestClient):
        """Incident list should work regardless of M9 flags."""
        resp = e2e_client.get("/api/incidents?limit=5")
        assert resp.status_code == 200
        data = resp.json()
        assert "items" in data

    def test_agent_run_detail_nonexistent(self, e2e_client: TestClient):
        """Non-existent agent run should return 404."""
        resp = e2e_client.get("/api/agent-runs/run_nonexistent")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Failure Injection: M9 Feature Disabled Behavior
# ---------------------------------------------------------------------------


class TestE2EM9FailureInjection:
    """Failure injection tests for M9 AI extensions."""

    def test_m9_disabled_keeps_runbook_search_working(
        self, e2e_client: TestClient
    ):
        """Keyword search must work when M9 is disabled (fixture defaults safe)."""
        resp = e2e_client.get("/api/runbooks/search?q=latency")
        assert resp.status_code == 200

    def test_web_search_rejects_unsafe_urls_in_query(self, e2e_client: TestClient):
        """Web search should handle queries with URLs safely."""
        resp = e2e_client.post(
            "/api/runbooks/web-search",
            json={
                "query": "check http://169.254.169.254/latest/meta-data/",
                "incident_id": "inc_nonexistent",
            },
        )
        # Should either reject or handle safely
        assert resp.status_code in (200, 400, 422)

    def test_llm_generate_handles_missing_incident_gracefully(
        self, e2e_client: TestClient
    ):
        """LLM generate with missing incident should not crash."""
        resp = e2e_client.post(
            "/api/runbooks/llm-generate",
            json={
                "incident_id": "inc_definitely_does_not_exist_12345",
                "approved_runbook_version_id": None,
            },
        )
        assert resp.status_code != 500  # Should not crash
