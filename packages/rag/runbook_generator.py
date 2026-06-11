"""LLM-powered runbook draft generation from incident clusters."""

from __future__ import annotations

import logging
from datetime import date

from packages.agent.llm.base import LLMProvider
from packages.db.repositories.runbook_drafts import RunbookDraftRepository
from packages.rag.template_extractor import TemplateCandidate, TemplateExtractor

logger = logging.getLogger(__name__)

RUNBOOK_GENERATION_PROMPT = """\
Generate an SRE runbook in Markdown format with YAML front matter.

Service: {service}
Incident Type: {incident_type}
Based on {incident_count} resolved incidents.

Common Root Causes:
{root_causes}

Common Mitigation Actions:
{actions}

Common Evidence Collected:
{evidence_types}

Generate a runbook with these sections:
- A level-1 heading title
- ## Detection (H2): how to detect this incident type
- ## Evidence To Collect (H2): what evidence to gather
- ## Initial Decision (H2): how to decide on the response

The front matter must include: service, incident_type, severity, owner,
updated_at (today's date in YYYY-MM-DD).

Return ONLY the Markdown, with no surrounding explanation."""


class RunbookGenerator:
    """Generate runbook drafts using LLM from historical incident clusters."""

    def __init__(
        self,
        llm: LLMProvider,
        draft_repo: RunbookDraftRepository,
        extractor: TemplateExtractor,
    ) -> None:
        self.llm = llm
        self.draft_repo = draft_repo
        self.extractor = extractor

    def generate_draft(self, candidate: TemplateCandidate) -> str | None:
        """Generate a runbook draft for a single template candidate.

        Returns the draft_id if created, or None if skipped (already exists).
        """
        if self.draft_repo.has_draft_for_fingerprint(candidate.fingerprint):
            return None

        prompt = RUNBOOK_GENERATION_PROMPT.format(
            service=candidate.service,
            incident_type=candidate.incident_type,
            incident_count=candidate.incident_count,
            root_causes="\n".join(f"- {rc}" for rc in candidate.common_root_causes)
            or "- Unknown",
            actions="\n".join(f"- {a}" for a in candidate.common_actions)
            or "- Investigate",
            evidence_types="\n".join(f"- {e}" for e in candidate.common_evidence_types)
            or "- Metrics, logs, traces",
        )

        try:
            content = self.llm.invoke(
                [{"role": "user", "content": prompt}],
                thinking=False,
            )
        except Exception:
            logger.warning(
                "LLM invocation failed for fingerprint=%s", candidate.fingerprint, exc_info=True
            )
            return None

        if not content or len(content.strip()) < 50:
            logger.warning(
                "LLM returned insufficient content for fingerprint=%s (len=%d)",
                candidate.fingerprint,
                len(content.strip()) if content else 0,
            )
            return None

        draft = self.draft_repo.create(
            fingerprint=candidate.fingerprint,
            incident_ids=[],
            service=candidate.service,
            incident_type=candidate.incident_type,
            title=f"{candidate.incident_type} Runbook Draft",
            content=content.strip(),
            front_matter={
                "service": candidate.service,
                "incident_type": candidate.incident_type,
                "severity": "P2",
                "owner": "auto-generated",
                "updated_at": date.today().isoformat(),
            },
            llm_model=getattr(self.llm, "model_name", None),
        )
        return draft.draft_id

    def generate_all(
        self,
        *,
        min_incident_count: int = 3,
        fingerprint: str | None = None,
    ) -> list[str]:
        """Generate drafts for all qualifying fingerprints.

        Returns list of created draft_ids.
        """
        candidates = self.extractor.extract_candidates(
            min_incident_count=min_incident_count,
            fingerprint=fingerprint,
        )
        draft_ids: list[str] = []
        for candidate in candidates:
            draft_id = self.generate_draft(candidate)
            if draft_id:
                draft_ids.append(draft_id)
        return draft_ids
