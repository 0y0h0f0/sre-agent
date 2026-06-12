"""Incident Diff Analyzer — M9 PR 9.3.

LLM-powered analysis of differences between an incident and an approved runbook.
Only produces AmendmentProposals — never modifies the source runbook.

Evidence threshold: at least one of (diagnosis report, operator feedback,
action execution results, linked approved runbook version, >= MIN_EVIDENCE_REFS)
must be present before the LLM is invoked.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from packages.agent.llm.base import LLMProvider
from packages.common.feature_flags import is_m9_subfeature_enabled
from packages.common.metrics import llm_incident_diff_total
from packages.common.redaction import redact_text
from packages.common.settings import Settings

logger = logging.getLogger(__name__)

_MIN_EVIDENCE_REFS = 5

_DIFF_PROMPT_TEMPLATE = """\
Analyze the differences between an SRE incident and an approved runbook.

Service: {service}
Fault Type: {fault_type}

Approved Runbook:
{approved_runbook}

Incident Context:
{incident_context}

Identify gaps in the approved runbook. For each gap, propose a specific amendment:
- missing_step: A diagnostic or remediation step that the runbook lacks
- outdated_metric: A metric referenced in the runbook that is deprecated/wrong
- wrong_label_mapping: A label selector that does not match production
- missing_rollback: A required rollback step that the runbook omits
- unsafe_action_wording: An action that is worded in a potentially unsafe way
- insufficient_evidence: A section that lacks required evidence references

For each amendment, provide:
1. amendment_type (from the list above)
2. rationale (why this change is needed, based on the incident)
3. proposed_content (the suggested new or revised runbook text)
4. evidence_refs (specific incident evidence IDs supporting the change)
5. confidence (high if evidence is direct, low if speculative)

Return ONLY a JSON array of amendment objects, with no surrounding text."""


class AmendmentType(StrEnum):
    MISSING_STEP = "missing_step"
    OUTDATED_METRIC = "outdated_metric"
    WRONG_LABEL_MAPPING = "wrong_label_mapping"
    MISSING_ROLLBACK = "missing_rollback"
    UNSAFE_ACTION_WORDING = "unsafe_action_wording"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


@dataclass
class AmendmentProposal:
    amendment_type: AmendmentType
    rationale: str
    proposed_content: str
    evidence_refs: list[str] = field(default_factory=list)
    confidence: str = "low"  # "high" | "low"


@dataclass
class DiffResult:
    status: str
    # "generated" | "disabled" | "blocked" | "degraded" | "skipped_insufficient_evidence"
    proposals: list[AmendmentProposal] = field(default_factory=list)
    prompt_metadata: dict[str, Any] | None = None
    error_message: str | None = None


class IncidentDiffAnalyzer:
    """Analyze incident vs approved runbook differences via LLM.

    Only produces AmendmentProposals — never modifies the source runbook.
    Requires minimum evidence before invoking the LLM.
    """

    def __init__(self, *, settings: Settings, llm: LLMProvider) -> None:
        self.settings = settings
        self.llm = llm

    def analyze(
        self,
        *,
        service: str,
        fault_type: str,
        approved_runbook: str,
        diagnosis_report: str | None = None,
        operator_feedback: str | None = None,
        action_execution_results: list[dict[str, Any]] | None = None,
        linked_approved_runbook_version: str | None = None,
        evidence_refs: list[str] | None = None,
    ) -> DiffResult:
        """Analyze differences and return amendment proposals.

        Returns DiffResult — never raises on LLM failure (degraded).
        """
        # 1. Feature gate check.
        if not is_m9_subfeature_enabled(self.settings, "llm_incident_diff"):
            logger.info("LLM incident diff disabled by feature gate")
            llm_incident_diff_total.labels(status="disabled").inc()
            return DiffResult(status="disabled")

        # 2. External provider check.
        if self._is_external_provider() and not self.settings.llm_external_provider_allowed:
            logger.warning("External LLM blocked for incident diff")
            llm_incident_diff_total.labels(status="blocked").inc()
            return DiffResult(
                status="blocked",
                error_message="LLM_EXTERNAL_PROVIDER_ALLOWED=true required for external provider",
            )

        # 3. Minimum evidence threshold.
        if not self._has_minimum_evidence(
            diagnosis_report=diagnosis_report,
            operator_feedback=operator_feedback,
            action_results=action_execution_results,
            linked_version=linked_approved_runbook_version,
            evidence_refs=evidence_refs,
        ):
            llm_incident_diff_total.labels(status="skipped_insufficient_evidence").inc()
            return DiffResult(status="skipped_insufficient_evidence")

        # 4. Build incident context.
        incident_context = self._build_context(
            diagnosis_report=diagnosis_report,
            operator_feedback=operator_feedback,
            action_results=action_execution_results,
            evidence_refs=evidence_refs,
        )

        # 5. Build redacted prompt.
        prompt = _DIFF_PROMPT_TEMPLATE.format(
            service=redact_text(service).redacted_text,
            fault_type=redact_text(fault_type).redacted_text,
            approved_runbook=approved_runbook,
            incident_context=redact_text(incident_context).redacted_text,
        )

        # 6. Invoke LLM.
        try:
            content = self.llm.invoke(
                [{"role": "user", "content": prompt}],
                thinking=False,
            )
        except Exception:
            logger.warning("LLM invocation failed for incident diff", exc_info=True)
            llm_incident_diff_total.labels(status="degraded").inc()
            return DiffResult(status="degraded", error_message="LLM invocation failed")

        if not content or len(content.strip()) < 20:
            llm_incident_diff_total.labels(status="degraded").inc()
            return DiffResult(status="degraded", error_message="LLM returned insufficient content")

        # 7. Parse proposals from LLM output.
        proposals = self._parse_proposals(content, evidence_refs or [])

        # 8. Build metadata.
        metadata: dict[str, Any] = {
            "prompt_template_version": "m9-9.3-1",
            "generated_output_hash": hashlib.sha256(content.encode()).hexdigest()[:16],
            "model_provider": getattr(self.llm, "model_name", "unknown"),
        }

        llm_incident_diff_total.labels(status="generated").inc()
        return DiffResult(
            status="generated",
            proposals=proposals,
            prompt_metadata=metadata,
        )

    # --- Internal helpers ---

    @staticmethod
    def _has_minimum_evidence(
        *,
        diagnosis_report: str | None = None,
        operator_feedback: str | None = None,
        action_results: list[dict[str, Any]] | None = None,
        linked_version: str | None = None,
        evidence_refs: list[str] | None = None,
    ) -> bool:
        if diagnosis_report and len(diagnosis_report.strip()) > 20:
            return True
        if operator_feedback and len(operator_feedback.strip()) > 10:
            return True
        if action_results and len(action_results) > 0:
            return True
        if linked_version:
            return True
        if evidence_refs and len(evidence_refs) >= _MIN_EVIDENCE_REFS:
            return True
        return False

    @staticmethod
    def _build_context(
        *,
        diagnosis_report: str | None = None,
        operator_feedback: str | None = None,
        action_results: list[dict[str, Any]] | None = None,
        evidence_refs: list[str] | None = None,
    ) -> str:
        parts: list[str] = []
        if diagnosis_report:
            parts.append(f"Diagnosis Report:\n{diagnosis_report}")
        if operator_feedback:
            parts.append(f"Operator Feedback:\n{operator_feedback}")
        if action_results:
            parts.append(f"Action Execution Results:\n{_format_list(action_results)}")
        if evidence_refs:
            parts.append(f"Evidence Refs: {', '.join(evidence_refs)}")
        return "\n\n".join(parts) if parts else "No incident context available."

    @staticmethod
    def _parse_proposals(content: str, available_evidence: list[str]) -> list[AmendmentProposal]:
        """Parse the LLM JSON output into AmendmentProposal objects.

        Falls back to a single low-confidence note when JSON parsing fails
        (e.g. with FakeLLM in tests, or degraded LLM output).
        """
        import json as _json

        try:
            raw = _json.loads(content.strip())
        except _json.JSONDecodeError:
            # Non-JSON output → synthesize a low-confidence reviewer note.
            logger.warning("Failed to parse LLM diff output as JSON — synthesizing note")
            return [_synthesize_note(content, available_evidence)]

        if not isinstance(raw, list) or len(raw) == 0:
            return [_synthesize_note(content, available_evidence)]

        proposals: list[AmendmentProposal] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            try:
                atype_str = str(item.get("amendment_type", ""))
                if atype_str not in AmendmentType:
                    continue
                refs = item.get("evidence_refs", [])
                if not isinstance(refs, list):
                    refs = []
                refs = [str(r) for r in refs]
                confidence = str(item.get("confidence", "low")).lower()
                if confidence not in ("high", "low"):
                    confidence = "low"
                # High confidence without evidence → downgrade to low
                if confidence == "high" and not refs:
                    confidence = "low"
                proposals.append(AmendmentProposal(
                    amendment_type=AmendmentType(atype_str),
                    rationale=str(item.get("rationale", ""))[:2000],
                    proposed_content=str(item.get("proposed_content", ""))[:5000],
                    evidence_refs=refs,
                    confidence=confidence,
                ))
            except (ValueError, TypeError):
                continue

        if not proposals:
            return [_synthesize_note(content, available_evidence)]
        return proposals

    def _is_external_provider(self) -> bool:
        provider = self.settings.llm_provider.strip().lower()
        return provider in ("openai", "deepseek", "anthropic")


def _synthesize_note(content: str, evidence_refs: list[str]) -> AmendmentProposal:
    """Create a low-confidence reviewer note from unstructured LLM output."""
    return AmendmentProposal(
        amendment_type=AmendmentType.INSUFFICIENT_EVIDENCE,
        rationale="LLM output could not be parsed as structured amendments. "
                  "Review the raw output for potential insights.",
        proposed_content=content[:1000],
        evidence_refs=evidence_refs,
        confidence="low",
    )


def _format_list(items: list[dict[str, Any]]) -> str:
    return "\n".join(
        f"- {item.get('action', 'unknown')}: {item.get('outcome', 'unknown')}"
        for item in items
    )
