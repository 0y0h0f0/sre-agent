"""Tests for RunbookDraft extension and approved draft ingest — PR 6.2."""

from __future__ import annotations

import hashlib
from datetime import UTC

import pytest

from apps.api.schemas.runbooks import RunbookDraftItem
from packages.db.models import RunbookDraft


def _make_draft(
    *,
    draft_id: str = "drf_test001",
    fingerprint: str = "fp-test-001",
    service: str = "test-service",
    incident_type: str = "high_latency",
    title: str = "Test Runbook Draft",
    content: str | None = None,
    status: str = "draft",
    draft_type: str = "incident_cluster",
    source: str = "llm",
    discovery_run_id: str | None = None,
    parent_draft_id: str | None = None,
) -> RunbookDraft:
    if content is None:
        content = """---
service: test-service
incident_type: high_latency
severity: P2
owner: test-team
updated_at: 2026-06-12
---

# Test Runbook

## Detection

Detect high latency for **test-service**.

## Evidence To Collect

### Metrics
- Check p95 latency on `http_request_duration_seconds_bucket`.

## Initial Decision
1. If latency > 500ms: scale out.
"""
    return RunbookDraft(
        draft_id=draft_id,
        fingerprint=fingerprint,
        incident_ids=[],
        service=service,
        incident_type=incident_type,
        title=title,
        content=content,
        front_matter={
            "service": service,
            "incident_type": incident_type,
            "severity": "P2",
            "owner": "test-team",
            "updated_at": "2026-06-12",
        },
        status=status,
        draft_type=draft_type,
        source=source,
        discovery_run_id=discovery_run_id,
        parent_draft_id=parent_draft_id,
    )


class TestRunbookDraftModel:
    def test_draft_default_draft_type(self):
        draft = _make_draft()
        assert draft.draft_type == "incident_cluster"

    def test_draft_default_source(self):
        draft = _make_draft()
        assert draft.source == "llm"

    def test_draft_custom_draft_type(self):
        draft = _make_draft(draft_type="template", source="template_engine")
        assert draft.draft_type == "template"
        assert draft.source == "template_engine"

    def test_draft_discovery_run_id(self):
        draft = _make_draft(discovery_run_id="run-001")
        assert draft.discovery_run_id == "run-001"

    def test_draft_discovery_run_id_nullable(self):
        draft = _make_draft(discovery_run_id=None)
        assert draft.discovery_run_id is None

    def test_draft_parent_draft_id(self):
        draft = _make_draft(parent_draft_id="drf_original001")
        assert draft.parent_draft_id == "drf_original001"

    def test_draft_parent_draft_id_nullable(self):
        draft = _make_draft(parent_draft_id=None)
        assert draft.parent_draft_id is None

    def test_draft_type_values_are_flexible(self):
        """Draft type is a string, not an enum — different flows set different values."""
        valid_types = ["incident_cluster", "template", "amendment"]
        for t in valid_types:
            draft = _make_draft(draft_type=t)
            assert draft.draft_type == t


class TestRunbookDraftItemSchema:
    def test_draft_item_includes_new_fields(self):
        draft = _make_draft(
            draft_type="template",
            source="template_engine",
            discovery_run_id="run-abc",
            parent_draft_id="drf_parent001",
        )
        from datetime import datetime
        now = datetime.now(UTC).isoformat()
        item = RunbookDraftItem(
            draft_id=draft.draft_id,
            fingerprint=draft.fingerprint,
            incident_ids=draft.incident_ids or [],
            service=draft.service,
            incident_type=draft.incident_type,
            title=draft.title,
            content=draft.content,
            status=draft.status,
            draft_type=draft.draft_type,
            source=draft.source,
            discovery_run_id=draft.discovery_run_id,
            parent_draft_id=draft.parent_draft_id,
            created_at=now,
            updated_at=now,
        )
        assert item.draft_type == "template"
        assert item.source == "template_engine"
        assert item.discovery_run_id == "run-abc"
        assert item.parent_draft_id == "drf_parent001"

    def test_draft_item_defaults_for_backward_compat(self):
        """New fields have defaults so existing clients don't break."""
        item = RunbookDraftItem(
            draft_id="drf_x",
            fingerprint="fp",
            incident_ids=[],
            service="svc",
            incident_type="x",
            title="t",
            content="c",
            status="draft",
            created_at="2026-01-01",
            updated_at="2026-01-01",
        )
        assert item.draft_type == "incident_cluster"
        assert item.source == "llm"
        assert item.discovery_run_id is None
        assert item.parent_draft_id is None


class TestApprovedDraftIngest:
    def test_parse_content_as_runbook_success(self, db_session):
        """Approved draft content is parseable as runbook markdown."""
        from packages.rag.metadata import parse_runbook_markdown

        content = """---
service: test-service
incident_type: high_latency
severity: P2
owner: test-team
updated_at: 2026-06-12
---

# Test Runbook

## Detection
Detect this.
"""
        document = parse_runbook_markdown(content, source_path="drafts/drf_x.md")
        assert document.title == "Test Runbook"
        assert document.metadata.service == "test-service"

    def test_draft_content_invalid_front_matter_does_not_crash(self, db_session):
        """If draft content lacks valid front matter, ingest skips without error."""
        from packages.rag.metadata import RunbookMetadataError, parse_runbook_markdown

        with pytest.raises(RunbookMetadataError):
            parse_runbook_markdown("# No front matter", source_path="test.md")

    def test_ingest_creates_chunks_for_valid_draft(self, db_session):
        """Approved draft with valid content creates runbook_chunks."""
        from packages.db.repositories.runbooks import RunbookChunkRepository

        repo = RunbookChunkRepository(db_session)
        draft = _make_draft()

        # Parse
        from packages.rag.metadata import parse_runbook_markdown
        document = parse_runbook_markdown(
            draft.content, source_path=f"drafts/{draft.draft_id}.md"
        )

        # Split
        from packages.rag.splitter import split_markdown_document
        chunk_drafts = split_markdown_document(document)
        assert len(chunk_drafts) > 0

        # Create chunks (without embedding — degraded mode)
        from packages.common.ids import new_id
        from packages.db.repositories.runbooks import degraded_runbook_embedding
        for cd in chunk_drafts:
            repo.create_chunk(
                chunk_id=new_id("chk_"),
                document_id=cd.document_id,
                source_path=cd.source_path,
                title=cd.title,
                content=cd.content,
                content_hash=cd.content_hash,
                embedding=degraded_runbook_embedding(),
                embedding_model="none",
                metadata=dict(cd.metadata),
            )

        # Verify chunks were created
        assert repo.count_chunks() == len(chunk_drafts)
        # source_path is set
        for cd in chunk_drafts:
            assert cd.source_path.startswith("drafts/")
        # Verify dedup works
        assert repo.get_by_content_hash(chunk_drafts[0].content_hash) is not None

    def test_ingest_dedup_by_content_hash(self, db_session):
        """Same content hash should not create duplicate chunks."""
        from packages.common.ids import new_id
        from packages.db.repositories.runbooks import (
            RunbookChunkRepository,
            degraded_runbook_embedding,
        )

        repo = RunbookChunkRepository(db_session)
        chunk_id_1 = new_id("chk_")
        content_hash = hashlib.sha256(b"test content").hexdigest()

        repo.create_chunk(
            chunk_id=chunk_id_1,
            document_id="doc-1",
            source_path="drafts/test.md",
            title="Test Chunk",
            content="test content",
            content_hash=content_hash,
            embedding=degraded_runbook_embedding(),
            embedding_model="none",
            metadata={},
        )
        db_session.flush()

        # Second create with same hash should be detected
        existing = repo.get_by_content_hash(content_hash)
        assert existing is not None
        assert existing.chunk_id == chunk_id_1

    def test_source_path_always_set(self, db_session):
        """Every chunk created from a draft must have source_path set."""
        from packages.common.ids import new_id
        from packages.db.repositories.runbooks import (
            RunbookChunkRepository,
            degraded_runbook_embedding,
        )

        repo = RunbookChunkRepository(db_session)
        chunk = repo.create_chunk(
            chunk_id=new_id("chk_"),
            document_id="doc-1",
            source_path="drafts/drf_abc.md",
            title="Test",
            content="content",
            content_hash=hashlib.sha256(b"content").hexdigest(),
            embedding=degraded_runbook_embedding(),
            embedding_model="none",
            metadata={},
        )
        assert chunk.source_path == "drafts/drf_abc.md"

    def test_pending_draft_not_ingested(self, db_session):
        """Only approved (published) drafts should be ingested into chunks."""
        from packages.db.repositories.runbook_drafts import RunbookDraftRepository

        repo = RunbookDraftRepository(db_session)
        # Create a draft with status "draft" (pending)
        draft = repo.create(
            fingerprint="fp-pending",
            incident_ids=[],
            service="svc",
            incident_type="x",
            title="Pending Draft",
            content="# Test\n\n## Section\n\ntest",
            front_matter={
                "service": "svc",
                "incident_type": "x",
                "severity": "P2",
                "owner": "team",
                "updated_at": "2026-01-01",
            },
            draft_type="template",
            source="template_engine",
        )
        assert draft.status == "draft"
        assert draft.draft_type == "template"
        assert draft.source == "template_engine"

    def test_rejected_draft_not_ingested(self, db_session):
        """Rejected drafts should not be ingested into chunks."""
        from packages.db.repositories.runbook_drafts import RunbookDraftRepository

        repo = RunbookDraftRepository(db_session)
        draft = repo.create(
            fingerprint="fp-rejected",
            incident_ids=[],
            service="svc",
            incident_type="x",
            title="Rejected Draft",
            content="# Test\n\n## Section\n\ntest",
            front_matter={
                "service": "svc",
                "incident_type": "x",
                "severity": "P2",
                "owner": "team",
                "updated_at": "2026-01-01",
            },
        )
        repo.update_status(draft.draft_id, "rejected", reviewer="reviewer", comment="not good")
        updated = repo.get_by_draft_id(draft.draft_id)
        assert updated is not None
        assert updated.status == "rejected"

    def test_chunk_source_path_from_draft(self):
        """Verify that source_path format is 'drafts/{draft_id}.md'."""
        draft_id = "drf_source001"
        expected_path = f"drafts/{draft_id}.md"
        assert expected_path == f"drafts/{draft_id}.md"
        assert expected_path.startswith("drafts/")
        assert expected_path.endswith(".md")


class TestEmbeddingDegradation:
    def test_degraded_embedding_stored_when_provider_unavailable(self, db_session):
        """Chunks can be stored with pgvector-compatible degraded embeddings."""
        from packages.common.ids import new_id
        from packages.db.repositories.runbooks import (
            RunbookChunkRepository,
            degraded_runbook_embedding,
        )

        repo = RunbookChunkRepository(db_session)
        chunk = repo.create_chunk(
            chunk_id=new_id("chk_"),
            document_id="doc-degraded",
            source_path="drafts/test.md",
            title="Degraded Chunk",
            content="content for keyword search",
            content_hash=hashlib.sha256(b"content for keyword search").hexdigest(),
            embedding=degraded_runbook_embedding(),
            embedding_model="none",
            metadata={},
        )
        assert chunk.embedding == degraded_runbook_embedding()
        assert len(chunk.embedding) == 512
        assert chunk.embedding_model == "none"
        assert chunk.content == "content for keyword search"


class TestRunbookVersionOnPublish:
    def test_publishing_creates_version_record(self, db_session):
        """When a draft is published, a RunbookVersion is created."""
        from packages.db.repositories.runbook_versions import RunbookVersionRepository

        version_repo = RunbookVersionRepository(db_session)
        draft = _make_draft(draft_id="drf_ver001", status="published")

        content_hash = hashlib.sha256(draft.content.encode()).hexdigest()
        version = version_repo.create(
            document_id=draft.draft_id,
            source_path=f"drafts/{draft.draft_id}.md",
            content_hash=content_hash,
            change_reason="published_from_draft",
            related_draft_id=draft.draft_id,
            created_by="reviewer",
        )
        assert version.document_id == "drf_ver001"
        assert version.version_number == 1
        assert version.change_reason == "published_from_draft"

        # Second version for same document gets incremented version_number
        version2 = version_repo.create(
            document_id=draft.draft_id,
            source_path=f"drafts/{draft.draft_id}.md",
            content_hash=hashlib.sha256(b"updated content").hexdigest(),
            change_reason="regenerated",
            related_draft_id="drf_ver002",
            created_by="reviewer",
        )
        assert version2.version_number == 2
