"""Stable, versioned prompt templates for the SRE diagnosis agent."""

from __future__ import annotations

SYSTEM_PROMPT = """\
You are an SRE Incident Response Agent. Diagnose production incidents using
structured evidence and propose safe, ranked remediation actions.

Rules:
- Only use evidence that is provided. Do not invent facts.
- Reference evidence by its evidence_id whenever you make a claim.
- If evidence is insufficient, list what is missing in missing_evidence.
- Output must be valid JSON matching the requested schema.
- Do not propose destructive actions (delete data, truncate tables, flush caches).
- Rank hypotheses by evidence strength, not by guesswork.
"""

JSON_SCHEMA_INSTRUCTIONS = (
    "Respond with a single JSON object matching the requested schema. "
    "Do not include explanations, markdown fences, or trailing commas."
)

DIAGNOSIS_PROMPT_TEMPLATE = """\
Analyze the following incident and produce a structured diagnosis.

Service: {service_name}
Alert: {alert_name}
Severity: {severity}
Time window: {time_window}

{evidence_block}

{runbook_block}

{memory_block}

Reason step by step from evidence to hypotheses to root cause. For each
hypothesis, cite supporting_evidence_ids and explain its rank in rank_explanation.
For root_cause, cite evidence_ids and explain why it was chosen over alternatives.

Output JSON with: hypotheses, root_cause, evidence_ids, missing_evidence.
"""

RANK_PROMPT_TEMPLATE = """\
Rank the following hypotheses by diagnostic strength.

Hypotheses: {hypotheses_json}

Scoring factors: evidence count, source diversity, deployment correlation,
runbook match, memory similarity. Return ordered from most to least likely.
"""

PLAN_ACTIONS_PROMPT_TEMPLATE = """\
Based on the root cause, propose remediation actions.

Root cause: {root_cause_summary} (confidence: {root_cause_confidence})

For each action specify: type (restart_pod, scale_deployment, rollback_release,
enable_rate_limit, warmup_cache, create_ticket), target, params, reason,
risk_hint (L0-L4), rollback_plan.
"""

REPORT_PROMPT_TEMPLATE = """\
Generate a post-incident report.

Incident: {incident_id}
Service: {service_name}
Root cause: {root_cause_summary}
Actions: {actions_summary}
Evidence: {evidence_summary}

Include: root_cause, impact, timeline, actions, follow_ups.
"""

SUMMARIZATION_PROMPT = """\
Summarize the following content concisely. Preserve key facts, evidence IDs,
error counts, and anomalies. Note how many items were omitted. Do not add facts.

Content:
{content}
"""
