"""Integration coverage for M9 PR 9.3 amendment draft review."""

from __future__ import annotations

import hashlib
import json
import secrets
from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from apps.api.dependencies import get_app_settings
from packages.common.settings import Settings, get_settings
from packages.db.base import Base
from packages.db.models import AmendmentDraft, ApiKey, AuditLog


class StaticAmendmentLLM:
    provider = "fake"

    def invoke(
        self, messages: list[dict[str, Any]], *, thinking: bool = False, **kwargs: Any
    ) -> str:
        return json.dumps([
            {
                "amendment_type": "missing_step",
                "rationale": "Incident evidence showed the runbook missed DB pool checks.",
                "proposed_content": "Check DB pool saturation before restarting pods.",
                "evidence_refs": ["evd_pool"],
                "confidence": "high",
            }
        ])

    def generate_json(
        self, prompt: str, output_schema: Any, *, thinking: bool = False, **kwargs: Any
    ) -> Any:
        raise AssertionError("incident diff should call invoke(), not generate_json()")


@pytest.fixture()
def m9_client(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
    test_settings: Settings,
) -> TestClient:
    from apps.api.dependencies import get_db
    from apps.api.main import create_app

    settings = test_settings.model_copy(
        update={
            "m9_extensions_enabled": True,
            "llm_incident_diff_enabled": True,
            "llm_provider": "fake",
            "api_key_auth_enabled": False,
        }
    )

    app = create_app()

    def override_db():
        yield db_session

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_app_settings] = lambda: settings
    monkeypatch.setattr(
        "apps.api.routers.runbooks.build_llm",
        lambda _settings: StaticAmendmentLLM(),
    )
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()


def _create_amendment(
    session: Session,
    *,
    amendment_id: str = "amd_test_review",
    status: str = "pending_review",
    evidence_refs: list[str] | None = None,
    proposal_kind: str = "proposed_patch",
) -> AmendmentDraft:
    refs = ["evd_pool"] if evidence_refs is None else evidence_refs
    amendment = AmendmentDraft(
        amendment_id=amendment_id,
        summary_id=None,
        service="checkout",
        fault_type="high_5xx",
        source="llm_incident_diff",
        related_incident_id="inc_diff_test",
        runbook_version_id="rbv_test",
        amendment_type="missing_step",
        proposed_content="Check DB pool saturation before restart.",
        rationale="Incident evidence showed DB pool saturation.",
        evidence_incident_ids=refs,
        confidence="high" if refs else "low",
        proposal_kind=proposal_kind,
        status=status,
    )
    session.add(amendment)
    session.commit()
    return amendment


def test_incident_diff_creates_pending_amendment_and_audit_log(
    m9_client: TestClient,
    db_session: Session,
) -> None:
    response = m9_client.post(
        "/api/runbooks/incident-diff",
        json={
            "incident_id": "inc_diff_test",
            "approved_runbook_version_id": "rbv_approved",
            "service": "checkout",
            "fault_type": "high_5xx",
            "approved_runbook": "## Detection\nCheck error rate.",
            "diagnosis_report": "DB pool saturation was the confirmed root cause.",
            "evidence_refs": ["evd_pool"],
        },
        headers={"X-Request-Id": "req-diff-create"},
    )

    assert response.status_code == 200, response.text
    data = response.json()
    assert data["status"] == "generated"
    assert len(data["amendment_ids"]) == 1
    assert data["proposals"][0]["can_apply"] is True

    amendment = db_session.scalar(
        select(AmendmentDraft).where(
            AmendmentDraft.amendment_id == data["amendment_ids"][0]
        )
    )
    assert amendment is not None
    assert amendment.summary_id is None
    assert amendment.status == "pending_review"
    assert amendment.source == "llm_incident_diff"
    assert amendment.runbook_version_id == "rbv_approved"
    assert amendment.proposal_kind == "proposed_patch"

    audit = db_session.scalar(
        select(AuditLog).where(
            AuditLog.action == "runbook.amendment_draft.created",
            AuditLog.resource_id == amendment.amendment_id,
        )
    )
    assert audit is not None
    assert audit.actor == "anonymous"
    assert audit.source == "api"
    assert audit.request_id == "req-diff-create"
    assert audit.details["incident_id"] == "inc_diff_test"
    assert audit.details["runbook_version_id"] == "rbv_approved"


def test_amendment_status_pending_to_approved_to_applied(
    m9_client: TestClient,
    db_session: Session,
) -> None:
    amendment = _create_amendment(db_session)

    approved = m9_client.post(
        f"/api/runbooks/amendments/{amendment.amendment_id}/review",
        json={"status": "approved", "reviewer": "sr-reviewer"},
    )
    assert approved.status_code == 200, approved.text
    approved_body = approved.json()
    assert approved_body["status"] == "approved"
    assert approved_body["approved_by"] == "sr-reviewer"
    assert approved_body["approved_at"] is not None
    assert approved_body["applied_at"] is None

    applied = m9_client.post(
        f"/api/runbooks/amendments/{amendment.amendment_id}/review",
        json={
            "status": "applied",
            "reviewer": "sr-reviewer",
            "applied_to_draft_id": "drf_reviewed",
        },
    )
    assert applied.status_code == 200, applied.text
    applied_body = applied.json()
    assert applied_body["status"] == "applied"
    assert applied_body["approved_at"] is not None
    assert applied_body["applied_at"] is not None
    assert applied_body["applied_to_draft_id"] == "drf_reviewed"


def test_amendment_approved_does_not_mean_applied(
    m9_client: TestClient,
    db_session: Session,
) -> None:
    amendment = _create_amendment(db_session, amendment_id="amd_approved_only")

    response = m9_client.post(
        f"/api/runbooks/amendments/{amendment.amendment_id}/review",
        json={"status": "approved", "reviewer": "sr-reviewer"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "approved"
    assert body["applied_at"] is None
    assert body["applied_to_draft_id"] is None
    assert body["applied_to_runbook_version_id"] is None


def test_applied_amendment_requires_exactly_one_target(
    m9_client: TestClient,
    db_session: Session,
) -> None:
    amendment = _create_amendment(
        db_session,
        amendment_id="amd_two_apply_targets",
        status="approved",
    )

    response = m9_client.post(
        f"/api/runbooks/amendments/{amendment.amendment_id}/review",
        json={
            "status": "applied",
            "reviewer": "sr-reviewer",
            "applied_to_draft_id": "drf_reviewed",
            "applied_to_runbook_version_id": "rbv_reviewed",
        },
    )

    assert response.status_code == 400
    body = response.json()
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert "exactly one target" in body["error"]["message"]


def test_low_confidence_note_without_evidence_cannot_be_approved(
    m9_client: TestClient,
    db_session: Session,
) -> None:
    amendment = _create_amendment(
        db_session,
        amendment_id="amd_note_only",
        evidence_refs=[],
        proposal_kind="low_confidence_note",
    )

    response = m9_client.post(
        f"/api/runbooks/amendments/{amendment.amendment_id}/review",
        json={"status": "approved", "reviewer": "sr-reviewer"},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_approved_note_without_evidence_cannot_be_applied(
    m9_client: TestClient,
    db_session: Session,
) -> None:
    amendment = _create_amendment(
        db_session,
        amendment_id="amd_bad_apply",
        status="approved",
        evidence_refs=[],
        proposal_kind="low_confidence_note",
    )

    response = m9_client.post(
        f"/api/runbooks/amendments/{amendment.amendment_id}/review",
        json={
            "status": "applied",
            "reviewer": "sr-reviewer",
            "applied_to_draft_id": "drf_reviewed",
        },
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def _hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def _create_api_key(session: Session, *, scopes: list[str]) -> str:
    raw_key = secrets.token_hex(32)
    session.add(
        ApiKey(
            key_id=f"apik_diff_{secrets.token_hex(4)}",
            description="incident-diff-auth",
            key_hash=_hash_key(raw_key),
            scopes=scopes,
        )
    )
    session.commit()
    return raw_key


@pytest.fixture()
def incident_diff_auth_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    monkeypatch.setenv("API_KEY_AUTH_ENABLED", "true")
    get_settings.cache_clear()

    from apps.api.main import create_app

    app = create_app()
    settings = Settings(
        database_url="sqlite+pysqlite:///:memory:",
        api_key_auth_enabled=True,
        llm_provider="openai",
        llm_external_provider_allowed=True,
        m9_extensions_enabled=True,
        llm_incident_diff_enabled=True,
    )
    app.dependency_overrides[get_app_settings] = lambda: settings
    monkeypatch.setattr(
        "apps.api.routers.runbooks.build_llm",
        lambda _settings: StaticAmendmentLLM(),
    )

    with (
        patch("apps.api.middleware.auth.SessionLocal", TestSession),
        patch("packages.db.session.SessionLocal", TestSession),
    ):
        with TestClient(app) as client:
            client._test_session_factory = TestSession  # type: ignore[attr-defined]
            yield client

    app.dependency_overrides.clear()
    Base.metadata.drop_all(engine)
    engine.dispose()
    get_settings.cache_clear()


def _make_diff_payload() -> dict[str, object]:
    return {
        "service": "checkout",
        "fault_type": "high_5xx",
        "approved_runbook": "## Detection\nCheck error rate.",
        "diagnosis_report": "DB pool saturation was the confirmed root cause.",
        "evidence_refs": ["evd_pool"],
    }


def test_incident_diff_external_provider_requires_llm_invoke_scope(
    incident_diff_auth_client: TestClient,
) -> None:
    factory = incident_diff_auth_client._test_session_factory
    with factory() as session:
        raw = _create_api_key(
            session,
            scopes=["runbook:review", "incident:llm_diff"],
        )

    response = incident_diff_auth_client.post(
        "/api/runbooks/incident-diff",
        headers={"Authorization": f"Bearer {raw}"},
        json=_make_diff_payload(),
    )

    assert response.status_code == 403
    assert "llm:invoke" in response.json()["error"]["message"]


@pytest.mark.parametrize(
    ("scopes", "missing_scope"),
    [
        (["incident:llm_diff", "llm:invoke"], "runbook:review"),
        (["runbook:review", "llm:invoke"], "incident:llm_diff"),
    ],
)
def test_incident_diff_requires_base_scopes(
    incident_diff_auth_client: TestClient,
    scopes: list[str],
    missing_scope: str,
) -> None:
    factory = incident_diff_auth_client._test_session_factory
    with factory() as session:
        raw = _create_api_key(session, scopes=scopes)

    response = incident_diff_auth_client.post(
        "/api/runbooks/incident-diff",
        headers={"Authorization": f"Bearer {raw}"},
        json=_make_diff_payload(),
    )

    assert response.status_code == 403
    assert missing_scope in response.json()["error"]["message"]


def test_incident_diff_external_provider_allows_llm_invoke_scope(
    incident_diff_auth_client: TestClient,
) -> None:
    factory = incident_diff_auth_client._test_session_factory
    with factory() as session:
        raw = _create_api_key(
            session,
            scopes=["runbook:review", "incident:llm_diff", "llm:invoke"],
        )

    response = incident_diff_auth_client.post(
        "/api/runbooks/incident-diff",
        headers={"Authorization": f"Bearer {raw}"},
        json=_make_diff_payload(),
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "generated"
