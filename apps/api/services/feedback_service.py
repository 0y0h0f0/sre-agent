"""Feedback service — NFA marking, root cause correction, action correction, correlations."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from apps.api.schemas.common import Severity
from apps.api.schemas.feedback import (
    ActionCorrectionRequest,
    CorrelatedIncident,
    FeedbackResponse,
    NfaMarkRequest,
    NfaMarkResponse,
    RootCauseCorrectionRequest,
)
import json

from packages.common.errors import NotFoundError, ValidationAppError
from packages.common.settings import Settings
from packages.db.models import Incident
from packages.db.repositories.audit_logs import AuditLogRepository
from packages.db.repositories.false_positive_patterns import FalsePositivePatternRepository
from packages.db.repositories.feedback import FeedbackItemRepository
from packages.db.repositories.incident_correlations import IncidentCorrelationRepository
from packages.db.repositories.incidents import IncidentRepository
from packages.memory.memory_store import MemoryStore
from packages.memory.schemas import MemoryItemCreate


class FeedbackService:
    def __init__(
        self,
        db: Session,
        settings: Settings,
        memory_store: MemoryStore | None = None,
    ) -> None:
        self.db = db
        self.settings = settings
        self.incidents = IncidentRepository(db)
        self.fpp = FalsePositivePatternRepository(db)
        self.feedback_repo = FeedbackItemRepository(db)
        self.correlations = IncidentCorrelationRepository(db)
        self.audit = AuditLogRepository(db)
        self.memory = memory_store

    def _write_memory(
        self,
        scope: str,
        scope_key: str,
        memory_type: str,
        content: str,
        content_json: dict[str, Any] | None = None,
        importance: float = 0.5,
        source_ref: str | None = None,
    ) -> None:
        """Best-effort memory write. Swallows all errors."""
        if self.memory is None:
            return
        try:
            self.memory.put(MemoryItemCreate(
                scope=scope,
                scope_key=scope_key,
                memory_type=memory_type,
                content=content,
                content_json=content_json,
                importance=importance,
                source_ref=source_ref,
            ))
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 5.1 Cross-Incident Association
    # ------------------------------------------------------------------

    def get_correlated_incidents(self, incident_id: str) -> list[CorrelatedIncident]:
        incident = self._require_incident(incident_id)
        results: list[CorrelatedIncident] = []

        same_fp = self.correlations.find_by_fingerprint(
            incident.fingerprint, exclude_incident_id=incident_id, limit=5
        )
        for related in same_fp:
            results.append(
                CorrelatedIncident(
                    incident_id=related.incident_id,
                    service=related.service,
                    severity=related.severity,
                    alert_name=related.alert_name,
                    root_cause_summary=related.root_cause_summary,
                    correlation_type="same_fingerprint",
                    similarity_score=None,
                    created_at=related.created_at,
                )
            )

        if len(results) < self.settings.cross_incident_max_results:
            same_svc = self.correlations.find_similar_by_service(
                incident.service, exclude_incident_id=incident_id, limit=5
            )
            seen = {r.incident_id for r in results}
            for related in same_svc:
                if related.incident_id in seen:
                    continue
                results.append(
                    CorrelatedIncident(
                        incident_id=related.incident_id,
                        service=related.service,
                        severity=related.severity,
                        alert_name=related.alert_name,
                        root_cause_summary=related.root_cause_summary,
                        correlation_type="similar_service",
                        similarity_score=None,
                        created_at=related.created_at,
                    )
                )
                seen.add(related.incident_id)
                if len(results) >= self.settings.cross_incident_max_results:
                    break

        return results

    # ------------------------------------------------------------------
    # 5.2 False Positive Learning
    # ------------------------------------------------------------------

    def mark_nfa(
        self, incident_id: str, payload: NfaMarkRequest, submitted_by: str = "sre"
    ) -> NfaMarkResponse:
        incident = self._require_incident(incident_id)

        pattern = self.fpp.increment_nfa(
            fingerprint=incident.fingerprint,
            service=incident.service,
            alert_name=incident.alert_name,
            threshold=self.settings.nfa_auto_suppress_threshold,
        )

        self.feedback_repo.create(
            incident_id=incident_id,
            agent_run_id=None,
            feedback_type="nfa_mark",
            original_value={"fingerprint": incident.fingerprint, "service": incident.service},
            corrected_value={"nfa_count": pattern.nfa_count, "status": pattern.status},
            delta={"reason": payload.reason} if payload.reason else None,
            submitted_by=submitted_by,
        )
        self.audit.create(
            incident_id=incident_id,
            actor=submitted_by,
            action="nfa_mark",
            resource_type="incident",
            resource_id=incident_id,
            details={"reason": payload.reason, "nfa_count": pattern.nfa_count},
        )

        message = f"NFA recorded (count {pattern.nfa_count})"
        if pattern.status == "suppressed":
            message = f"Auto-suppressed after {pattern.nfa_count} NFA marks"
            incident.severity = Severity.P4.value

        # Write to memory store before commit
        self._write_memory(
            scope="service",
            scope_key=incident.service,
            memory_type="semantic",
            content=json.dumps({
                "fingerprint": incident.fingerprint,
                "alert_name": incident.alert_name,
                "nfa_count": pattern.nfa_count,
                "status": pattern.status,
                "reason": payload.reason,
            }),
            content_json={"service": incident.service, "nfa": True},
            importance=0.7,
            source_ref=f"incident:{incident_id}",
        )

        self.db.commit()

        return NfaMarkResponse(
            pattern_id=pattern.pattern_id,
            fingerprint=pattern.fingerprint,
            nfa_count=pattern.nfa_count,
            status=pattern.status,
            message=message,
        )

    # ------------------------------------------------------------------
    # 5.3 User Feedback Loop
    # ------------------------------------------------------------------

    def correct_root_cause(
        self,
        incident_id: str,
        payload: RootCauseCorrectionRequest,
        submitted_by: str = "sre",
    ) -> FeedbackResponse:
        incident = self._require_incident(incident_id)
        original = incident.root_cause_summary or "(not set)"
        corrected = payload.corrected_summary

        delta: dict[str, Any] = {
            "original": original,
            "corrected": corrected,
            "reason": payload.reason,
        }

        incident.root_cause_summary = corrected

        feedback = self.feedback_repo.create(
            incident_id=incident_id,
            agent_run_id=None,
            feedback_type="root_cause_correction",
            original_value={"root_cause_summary": original},
            corrected_value={"root_cause_summary": corrected},
            delta=delta,
            submitted_by=submitted_by,
        )
        self.audit.create(
            incident_id=incident_id,
            actor=submitted_by,
            action="root_cause_correct",
            resource_type="incident",
            resource_id=incident_id,
            details={"original": original, "corrected": corrected},
        )
        self.db.commit()

        # Write corrected root cause to service memory
        self._write_memory(
            scope="service",
            scope_key=incident.service,
            memory_type="semantic",
            content=json.dumps({
                "alert_name": incident.alert_name,
                "fingerprint": incident.fingerprint,
                "root_cause": corrected,
                "original_root_cause": original,
                "corrected_by": submitted_by,
                "reason": payload.reason,
            }),
            content_json={"service": incident.service, "corrected": True},
            importance=0.8,
            source_ref=f"incident:{incident_id}",
        )

        return FeedbackResponse(
            feedback_id=feedback.feedback_id,
            incident_id=feedback.incident_id,
            feedback_type=feedback.feedback_type,
            original_value=feedback.original_value,
            corrected_value=feedback.corrected_value,
            delta=feedback.delta,
            submitted_by=feedback.submitted_by,
            submitted_at=feedback.submitted_at,
        )

    def correct_action(
        self,
        incident_id: str,
        action_id: str | None,
        payload: ActionCorrectionRequest,
        submitted_by: str = "sre",
    ) -> FeedbackResponse:
        self._require_incident(incident_id)

        if payload.action_type == "add":
            if not payload.action:
                raise ValidationAppError("action is required when action_type is 'add'")
            original = None
            corrected = payload.action
            delta = {"added": payload.action, "reason": payload.reason}
        elif payload.action_type == "remove":
            if not payload.action_id:
                raise ValidationAppError("action_id is required when action_type is 'remove'")
            original = {"action_id": payload.action_id}
            corrected = None
            delta = {"removed_action_id": payload.action_id, "reason": payload.reason}
        else:
            raise ValidationAppError(
                f"invalid action_type: {payload.action_type}",
                details={"valid": ["add", "remove"]},
            )

        feedback = self.feedback_repo.create(
            incident_id=incident_id,
            agent_run_id=None,
            feedback_type=f"action_{payload.action_type}",
            original_value=original,
            corrected_value=corrected,
            delta=delta,
            submitted_by=submitted_by,
        )
        self.audit.create(
            incident_id=incident_id,
            actor=submitted_by,
            action=f"action_{payload.action_type}",
            resource_type="action",
            resource_id=payload.action_id or incident_id,
            details=delta,
        )
        self.db.commit()

        # Write procedural memory for added actions
        if payload.action_type == "add" and payload.action:
            incident = self._require_incident(incident_id)
            self._write_memory(
                scope="global",
                scope_key=f"action:{payload.action.get('type', 'unknown')}",
                memory_type="procedural",
                content=json.dumps({
                    "type": payload.action.get("type", ""),
                    "target": payload.action.get("target", ""),
                    "reason": payload.reason,
                    "service": incident.service,
                }),
                content_json={"service": incident.service},
                importance=0.6,
                source_ref=f"incident:{incident_id}",
            )

        return FeedbackResponse(
            feedback_id=feedback.feedback_id,
            incident_id=feedback.incident_id,
            feedback_type=feedback.feedback_type,
            original_value=feedback.original_value,
            corrected_value=feedback.corrected_value,
            delta=feedback.delta,
            submitted_by=feedback.submitted_by,
            submitted_at=feedback.submitted_at,
        )

    def list_feedback(self, incident_id: str) -> list[FeedbackResponse]:
        self._require_incident(incident_id)
        items = self.feedback_repo.list_for_incident(incident_id)
        return [
            FeedbackResponse(
                feedback_id=item.feedback_id,
                incident_id=item.incident_id,
                feedback_type=item.feedback_type,
                original_value=item.original_value,
                corrected_value=item.corrected_value,
                delta=item.delta,
                submitted_by=item.submitted_by,
                submitted_at=item.submitted_at,
            )
            for item in items
        ]

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _require_incident(self, incident_id: str) -> Incident:
        incident = self.incidents.get_by_public_id(incident_id)
        if incident is None:
            raise NotFoundError("incident", incident_id)
        return incident
