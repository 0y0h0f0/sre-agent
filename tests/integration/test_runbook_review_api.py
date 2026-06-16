"""Integration tests for Runbook Review API — PR 6.3.

Tests regenerate (never overwrites original) and template generation endpoints.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client_with_drafts(db_session):
    """Create a TestClient with drafts pre-populated in the DB."""
    from apps.api.dependencies import get_db
    from apps.api.main import app
    from packages.db.repositories.runbook_drafts import RunbookDraftRepository

    repo = RunbookDraftRepository(db_session)

    # Create an original draft
    original = repo.create(
        fingerprint="fp-regenerate-test",
        incident_ids=[],
        service="test-svc",
        incident_type="high_latency",
        title="Original Draft",
        content="""---
service: test-svc
incident_type: high_latency
severity: P2
owner: test-team
updated_at: 2026-06-12
---

# Original Draft

## Detection
Detect this.
""",
        front_matter={
            "service": "test-svc",
            "incident_type": "high_latency",
            "severity": "P2",
            "owner": "test-team",
            "updated_at": "2026-06-12",
        },
        source="llm",
        draft_type="incident_cluster",
    )
    db_session.flush()

    # Create a published draft
    published = repo.create(
        fingerprint="fp-published-test",
        incident_ids=[],
        service="pub-svc",
        incident_type="high_error_rate",
        title="Published Draft",
        content="""---
service: pub-svc
incident_type: high_error_rate
severity: P1
owner: platform
updated_at: 2026-06-12
---

# Published Draft

## Detection
Detect this.
""",
        front_matter={
            "service": "pub-svc",
            "incident_type": "high_error_rate",
            "severity": "P1",
            "owner": "platform",
            "updated_at": "2026-06-12",
        },
        source="llm",
        draft_type="incident_cluster",
    )
    repo.update_status(published.draft_id, "published", reviewer="reviewer")
    db_session.flush()

    # Override the DB dependency
    app.dependency_overrides[get_db] = lambda: db_session
    client = TestClient(app)
    yield {
        "client": client,
        "original": original,
        "published": published,
        "db_session": db_session,
    }
    app.dependency_overrides.clear()


class TestRegenerateDraft:
    def test_regenerate_creates_new_draft(self, client_with_drafts):
        client = client_with_drafts["client"]
        original = client_with_drafts["original"]

        resp = client.post(
            f"/api/runbooks/drafts/{original.draft_id}/regenerate",
            json={"reviewer": "operator", "comment": "regenerating for update"},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        # New draft has different ID
        assert data["draft_id"] != original.draft_id
        # New draft has parent set
        assert data["parent_draft_id"] == original.draft_id
        # New draft is pending (status="draft")
        assert data["status"] == "draft"
        # New draft title indicates regeneration
        assert "Regenerated" in data["title"]

    def test_regenerate_does_not_modify_original(self, client_with_drafts):
        client = client_with_drafts["client"]
        original = client_with_drafts["original"]

        # Regenerate
        resp = client.post(
            f"/api/runbooks/drafts/{original.draft_id}/regenerate",
            json={"reviewer": "operator"},
        )
        assert resp.status_code == 200

        # Fetch original again
        resp2 = client.get(f"/api/runbooks/drafts/{original.draft_id}")
        assert resp2.status_code == 200
        original_data = resp2.json()
        assert original_data["status"] == "draft"  # unchanged
        assert original_data["parent_draft_id"] is None  # original has no parent

    def test_regenerate_inherits_original_properties(self, client_with_drafts):
        client = client_with_drafts["client"]
        original = client_with_drafts["original"]

        resp = client.post(
            f"/api/runbooks/drafts/{original.draft_id}/regenerate",
            json={"reviewer": "operator"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["service"] == original.service
        assert data["incident_type"] == original.incident_type
        assert data["fingerprint"] == original.fingerprint
        assert data["content"] == original.content

    def test_regenerate_nonexistent_returns_404(self, client_with_drafts):
        client = client_with_drafts["client"]
        resp = client.post(
            "/api/runbooks/drafts/drf_nonexistent/regenerate",
            json={"reviewer": "operator"},
        )
        assert resp.status_code == 404

    def test_regenerate_then_review_new_draft(self, client_with_drafts):
        client = client_with_drafts["client"]
        original = client_with_drafts["original"]

        # Regenerate
        resp = client.post(
            f"/api/runbooks/drafts/{original.draft_id}/regenerate",
            json={"reviewer": "operator"},
        )
        assert resp.status_code == 200
        new_draft_id = resp.json()["draft_id"]

        # Approve the regenerated draft
        resp2 = client.post(
            f"/api/runbooks/drafts/{new_draft_id}/review",
            json={"status": "published", "reviewer": "sr-reviewer", "comment": "LGTM"},
        )
        assert resp2.status_code == 200, resp2.text
        assert resp2.json()["status"] == "published"

    def test_reject_regenerated_draft_does_not_affect_original(self, client_with_drafts):
        client = client_with_drafts["client"]
        original = client_with_drafts["original"]

        # Regenerate
        resp = client.post(
            f"/api/runbooks/drafts/{original.draft_id}/regenerate",
            json={"reviewer": "operator"},
        )
        new_draft_id = resp.json()["draft_id"]

        # Reject the regenerated draft
        client.post(
            f"/api/runbooks/drafts/{new_draft_id}/review",
            json={"status": "rejected", "reviewer": "sr-reviewer"},
        )

        # Original remains untouched
        resp3 = client.get(f"/api/runbooks/drafts/{original.draft_id}")
        assert resp3.json()["status"] == "draft"


class TestTemplateGenerate:
    def test_template_generate_creates_draft(self, client_with_drafts):
        client = client_with_drafts["client"]

        resp = client.post(
            "/api/runbooks/template",
            json={
                "service_name": "api-gateway",
                "incident_type": "high_latency",
                "title": "API Gateway Latency Runbook",
                "severity": "P1",
                "owner": "gateway-team",
            },
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["draft_id"].startswith("drf_")
        assert data["service_name"] == "api-gateway"
        assert data["incident_type"] == "high_latency"

    def test_template_generate_defaults_title(self, client_with_drafts):
        client = client_with_drafts["client"]

        resp = client.post(
            "/api/runbooks/template",
            json={
                "service_name": "worker-pool",
                "incident_type": "resource_saturation",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "Resource Saturation" in data["title"]

    def test_template_generate_creates_distinct_drafts(self, client_with_drafts):
        client = client_with_drafts["client"]

        resp1 = client.post(
            "/api/runbooks/template",
            json={
                "service_name": "svc-a",
                "incident_type": "high_error_rate",
            },
        )
        resp2 = client.post(
            "/api/runbooks/template",
            json={
                "service_name": "svc-a",
                "incident_type": "high_error_rate",
            },
        )
        assert resp1.json()["draft_id"] != resp2.json()["draft_id"]

    def test_template_generate_draft_is_pending_review(self, client_with_drafts):
        client = client_with_drafts["client"]

        resp = client.post(
            "/api/runbooks/template",
            json={
                "service_name": "svc-b",
                "incident_type": "dependency_failure",
            },
        )
        draft_id = resp.json()["draft_id"]

        # Fetch the draft
        resp2 = client.get(f"/api/runbooks/drafts/{draft_id}")
        assert resp2.json()["status"] == "draft"
        assert resp2.json()["draft_type"] == "template"
        assert resp2.json()["source"] == "template_engine"

    def test_template_generate_content_is_deterministic(self, client_with_drafts):
        client = client_with_drafts["client"]

        resp1 = client.post(
            "/api/runbooks/template",
            json={
                "service_name": "deterministic-svc",
                "incident_type": "high_latency",
                "severity": "P2",
                "owner": "test",
            },
        )
        resp2 = client.post(
            "/api/runbooks/template",
            json={
                "service_name": "deterministic-svc",
                "incident_type": "high_latency",
                "severity": "P2",
                "owner": "test",
            },
        )

        # Both drafts should have the same structural sections
        draft1 = client.get(f"/api/runbooks/drafts/{resp1.json()['draft_id']}")
        draft2 = client.get(f"/api/runbooks/drafts/{resp2.json()['draft_id']}")
        c1 = draft1.json()["content"]
        c2 = draft2.json()["content"]

        # Both contain same key sections (deterministic templates)
        for section in ["## Detection", "## Evidence To Collect", "## Initial Decision"]:
            assert section in c1
            assert section in c2

    def test_template_generate_validation_rejects_empty_service(self, client_with_drafts):
        client = client_with_drafts["client"]
        resp = client.post(
            "/api/runbooks/template",
            json={"service_name": "", "incident_type": "high_latency"},
        )
        assert resp.status_code == 422

    def test_template_generate_validation_rejects_empty_incident_type(self, client_with_drafts):
        client = client_with_drafts["client"]
        resp = client.post(
            "/api/runbooks/template",
            json={"service_name": "svc", "incident_type": ""},
        )
        assert resp.status_code == 422


class TestListDraftsWithNewFields:
    def test_list_drafts_includes_new_fields(self, client_with_drafts):
        client = client_with_drafts["client"]

        resp = client.get("/api/runbooks/drafts")
        assert resp.status_code == 200
        drafts = resp.json()
        assert len(drafts) >= 2

        for draft in drafts:
            assert "draft_type" in draft
            assert "source" in draft
            assert "discovery_run_id" in draft
            assert "parent_draft_id" in draft

    def test_get_draft_includes_new_fields(self, client_with_drafts):
        client = client_with_drafts["client"]
        original = client_with_drafts["original"]

        resp = client.get(f"/api/runbooks/drafts/{original.draft_id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["draft_type"] == "incident_cluster"
        assert data["source"] == "llm"
        assert data["parent_draft_id"] is None


class TestVersionOnPublish:
    def test_publish_creates_version(self, client_with_drafts):
        client = client_with_drafts["client"]
        original = client_with_drafts["original"]

        # Publish the draft
        resp = client.post(
            f"/api/runbooks/drafts/{original.draft_id}/review",
            json={"status": "published", "reviewer": "publisher"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "published"

        # Check versions
        resp2 = client.get(f"/api/runbooks/versions/{original.draft_id}")
        assert resp2.status_code == 200
        versions = resp2.json()
        assert len(versions) >= 1
        assert versions[0]["change_reason"] == "published_from_draft"
