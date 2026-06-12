"""LLM Runbook Generator — M9 PR 9.2.

Orchestrates LLM-powered runbook draft generation with full safety controls:
- Feature-gate check (M9 + RUNBOOK_LLM_GENERATION)
- Redacted prompt building
- LLM invocation
- Action step classification
- RunbookDraft creation (status=pending_review, draft_type=llm_generated)
- Audit-safe metadata recording

The LLM can ONLY produce drafts — never auto-approve, auto-publish, or
auto-apply amendments.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import Any

from packages.agent.llm.base import LLMProvider
from packages.common.feature_flags import is_m9_subfeature_enabled
from packages.common.settings import Settings
from packages.rag.runbook_action_classifier import (
    RunbookActionClassifier,
)
from packages.rag.runbook_prompt_builder import RunbookPromptBuilder

logger = logging.getLogger(__name__)

_MIN_CONTENT_LENGTH = 50


@dataclass
class LLMGenerationResult:
    """Result of an LLM runbook generation attempt."""

    status: str  # "generated" | "disabled" | "blocked" | "degraded"
    draft_id: str | None = None
    draft_status: str | None = None
    draft_type: str | None = None
    content: str | None = None
    prompt_metadata: dict[str, Any] | None = None
    action_classification_summary: dict[str, Any] | None = None
    error_message: str | None = None


class LLMRunbookGenerator:
    """Generate runbook drafts via LLM with full safety controls.

    The generator:
    1. Checks M9 feature gates (global + sub-feature)
    2. Validates external LLM provider constraints
    3. Builds a redacted prompt from approved context
    4. Invokes the LLM
    5. Classifies action steps for safety
    6. Creates a RunbookDraft(status=pending_review)

    It does NOT save to the database — that responsibility belongs to the
    caller (service/router layer). The generator is a pure domain component.
    """

    def __init__(
        self,
        *,
        settings: Settings,
        llm: LLMProvider,
        classifier: RunbookActionClassifier,
        prompt_builder: RunbookPromptBuilder,
    ) -> None:
        self.settings = settings
        self.llm = llm
        self.classifier = classifier
        self.prompt_builder = prompt_builder

    # (too many arguments — each is a distinct, optional context source)
    def generate(
        self,
        *,
        service: str,
        incident_type: str,
        runbook_context: list[str] | None = None,
        evidence_summary: str | None = None,
        template_draft: str | None = None,
        capability_gaps: list[str] | None = None,
        effective_config: dict[str, Any] | None = None,
        evidence_ids: list[str] | None = None,
    ) -> LLMGenerationResult:
        """Generate a runbook draft via LLM.

        Returns LLMGenerationResult — never raises on LLM failure (degraded).
        """
        # 1. Feature gate check.
        if not is_m9_subfeature_enabled(self.settings, "runbook_llm_generation"):
            logger.info("LLM runbook generation disabled by feature gate")
            return LLMGenerationResult(status="disabled")

        # 2. External provider check.
        if self._is_external_provider() and not self.settings.llm_external_provider_allowed:
            logger.warning(
                "LLM provider '%s' is external but LLM_EXTERNAL_PROVIDER_ALLOWED=false",
                self.settings.llm_provider,
            )
            return LLMGenerationResult(
                status="blocked",
                error_message=(
                    f"External LLM provider '{self.settings.llm_provider}' "
                    "requires LLM_EXTERNAL_PROVIDER_ALLOWED=true"
                ),
            )

        # 3. Build redacted prompt.
        prompt, metadata = self.prompt_builder.build(
            service=service,
            incident_type=incident_type,
            runbook_context=runbook_context,
            evidence_summary=evidence_summary,
            template_draft=template_draft,
            capability_gaps=capability_gaps,
            effective_config=effective_config,
        )
        if evidence_ids:
            metadata["evidence_ids"] = evidence_ids

        # 4. Invoke LLM.
        try:
            content = self.llm.invoke(
                [{"role": "user", "content": prompt}],
                thinking=False,
            )
        except Exception:
            logger.warning(
                "LLM invocation failed for service=%s incident_type=%s",
                service,
                incident_type,
                exc_info=True,
            )
            return LLMGenerationResult(
                status="degraded",
                error_message="LLM invocation failed",
            )

        if not content or len(content.strip()) < _MIN_CONTENT_LENGTH:
            logger.warning(
                "LLM returned insufficient content (len=%d)",
                len(content.strip()) if content else 0,
            )
            return LLMGenerationResult(
                status="degraded",
                error_message="LLM returned insufficient content",
            )

        # 5. Classify actions.
        steps = self.classifier.classify(content)
        action_summary = self.classifier.classification_summary(steps)

        # 6. Build metadata.
        metadata["generated_output_hash"] = hashlib.sha256(
            content.encode()
        ).hexdigest()[:16]
        metadata["model_provider"] = getattr(self.llm, "model_name", "unknown")
        metadata["prompt_template_id"] = self.prompt_builder.prompt_template_id

        return LLMGenerationResult(
            status="generated",
            content=content.strip(),
            draft_status="pending_review",
            draft_type="llm_generated",
            prompt_metadata=metadata,
            action_classification_summary=action_summary,
        )

    def _is_external_provider(self) -> bool:
        """Check whether the configured LLM provider is an external/cloud service."""
        provider = self.settings.llm_provider.strip().lower()
        return provider in ("openai", "deepseek", "anthropic")
