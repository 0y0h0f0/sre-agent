"""Runbook Prompt Builder — M9 PR 9.2.

Constructs redacted LLM prompts for runbook draft generation. Only approved
runbook chunks, incident evidence summaries, deterministic template drafts,
capability gaps, and redacted EffectiveConfig are allowed in prompts.

Raw secrets, tokens, passwords, private keys, auth headers, and backend
secrets are NEVER included in prompt text.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from packages.common.redaction import redact_dict_values, redact_text

# Version for tracking changes to the prompt template format.
_PROMPT_TEMPLATE_VERSION = "m9-9.2-1"

# Version for tracking changes to redaction rules.
_REDACTION_VERSION = "m9-9.2-1"

# Maximum characters for stored prompt preview.
_MAX_PROMPT_PREVIEW_CHARS = 4096

_RUNBOOK_GENERATION_PROMPT_TEMPLATE = """\
Generate an SRE runbook in Markdown format with YAML front matter.

Service: {service}
Incident Type: {incident_type}

{runbook_context_section}

{evidence_section}

{template_section}

{gaps_section}

{config_section}

The runbook must include these sections:
- A level-1 heading title
- ## Detection (H2): how to detect this incident type
- ## Evidence To Collect (H2): what evidence to gather
- ## Initial Decision (H2): how to decide on the response
- ## Actions (H2): numbered steps to mitigate the incident

The front matter must include: service, incident_type, severity, owner,
updated_at (today's date in YYYY-MM-DD).

Classify each action in ## Actions as safe diagnostic steps. Do NOT include
destructive actions (delete, drop, truncate, flush) under any circumstances.

Return ONLY the Markdown, with no surrounding explanation."""


class RunbookPromptBuilder:
    """Build redacted LLM prompts for runbook draft generation."""

    @property
    def prompt_template_id(self) -> str:
        return f"runbook-generation-{_PROMPT_TEMPLATE_VERSION}"

    @property
    def redaction_version(self) -> str:
        return _REDACTION_VERSION

    # (too many arguments by design — each is a distinct context source)
    def build(
        self,
        *,
        service: str,
        incident_type: str,
        runbook_context: list[str] | None = None,
        evidence_summary: str | None = None,
        template_draft: str | None = None,
        capability_gaps: list[str] | None = None,
        effective_config: dict[str, Any] | None = None,
    ) -> tuple[str, dict[str, Any]]:
        """Build a redacted prompt and return (prompt_text, metadata_dict).

        Args:
            service: Service name.
            incident_type: Incident fault type.
            runbook_context: Approved runbook chunks or excerpts for context.
            evidence_summary: Summary of incident evidence collected.
            template_draft: Deterministic template draft from RunbookTemplateEngine.
            capability_gaps: Known gaps in tool/capability coverage.
            effective_config: Redacted EffectiveConfig dict.

        Returns:
            Tuple of (prompt_string, metadata_dict).
        """
        # Build sections.
        runbook_context_section = self._build_runbook_context(runbook_context)
        evidence_section = self._build_evidence(evidence_summary)
        template_section = self._build_template(template_draft)
        gaps_section = self._build_gaps(capability_gaps)
        config_section = self._build_config(effective_config)

        # Redact service and incident_type too (belt-and-suspenders).
        service_redacted = redact_text(service).redacted_text
        itype_redacted = redact_text(incident_type).redacted_text

        prompt = _RUNBOOK_GENERATION_PROMPT_TEMPLATE.format(
            service=service_redacted,
            incident_type=itype_redacted,
            runbook_context_section=runbook_context_section,
            evidence_section=evidence_section,
            template_section=template_section,
            gaps_section=gaps_section,
            config_section=config_section,
        )

        # Compute metadata.
        input_hash = hashlib.sha256(
            json.dumps({
                "service": service,
                "incident_type": incident_type,
                "runbook_context_len": len(runbook_context or []),
                "evidence_len": len(evidence_summary or ""),
                "template_len": len(template_draft or ""),
                "gaps_count": len(capability_gaps or []),
            }, sort_keys=True).encode()
        ).hexdigest()[:16]

        preview = prompt[:_MAX_PROMPT_PREVIEW_CHARS]

        metadata: dict[str, Any] = {
            "prompt_template_id": self.prompt_template_id,
            "prompt_template_version": _PROMPT_TEMPLATE_VERSION,
            "redaction_version": _REDACTION_VERSION,
            "input_object_hash": input_hash,
            "evidence_ids": [],
            "generated_output_hash": "",  # filled after LLM call
            "model_provider": "",  # filled after LLM call
            "prompt_preview": preview,
        }

        return prompt, metadata

    # --- Section builders ---

    @staticmethod
    def _build_runbook_context(chunks: list[str] | None) -> str:
        if not chunks:
            return ""
        # Redact each chunk.
        redacted = [redact_text(c).redacted_text for c in chunks]
        joined = "\n".join(f"- {c}" for c in redacted)
        return f"Relevant approved runbook context:\n{joined}\n"

    @staticmethod
    def _build_evidence(summary: str | None) -> str:
        if not summary:
            return ""
        rr = redact_text(summary)
        return f"Incident Evidence Summary:\n{rr.redacted_text}\n"

    @staticmethod
    def _build_template(draft: str | None) -> str:
        if not draft:
            return ""
        return f"Deterministic template draft (for reference):\n{draft}\n"

    @staticmethod
    def _build_gaps(gaps: list[str] | None) -> str:
        if not gaps:
            return ""
        joined = "\n".join(f"- {g}" for g in gaps)
        return f"Known capability/tool gaps:\n{joined}\n"

    @staticmethod
    def _build_config(config: dict[str, Any] | None) -> str:
        if not config:
            return ""
        redacted_config, _ = redact_dict_values(config)
        config_str = json.dumps(redacted_config, indent=2, default=str)
        return f"Redacted EffectiveConfig:\n```json\n{config_str}\n```\n"
