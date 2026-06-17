"""Stable, versioned prompt templates for the SRE diagnosis agent.

Real-LLM tuned (Phase 3): added few-shot examples, allowed action types,
and structured output guidance while preserving FakeLLM compatibility.
"""

from __future__ import annotations

SYSTEM_PROMPT = """\
You are an SRE Incident Response Agent. Diagnose production incidents using
structured evidence and propose safe, ranked remediation actions.

Rules:
- Only use evidence that is provided. Do not invent facts.
- Reference evidence by its evidence_id whenever you make a claim.
- If evidence is insufficient, list what is missing in missing_evidence.
- Output must be valid JSON matching the requested schema — no prose, no markdown.
- Do not propose destructive actions (delete_data, truncate_table, flush_cache,
  modify_database). These are L4 and will be hard-rejected.
- Rank hypotheses by evidence strength, not by guesswork.
- If you are uncertain, reflect that in confidence scores (<0.5) rather than
  fabricating evidence.
"""

JSON_SCHEMA_INSTRUCTIONS = (
    "Respond with a single JSON object matching the requested schema. "
    "Do not include explanations, markdown fences, or trailing commas. "
    "If reasoning is requested, place it before the JSON — the JSON must be "
    "the final non-whitespace content of your response."
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

Output JSON with keys: hypotheses, root_cause, evidence_ids, missing_evidence.
Each hypothesis must have: id, statement, supporting_evidence_ids, confidence (0-1),
rank_explanation. Root cause must have: summary, confidence (0-1), evidence_ids.

Example structure (do not copy values — this is a format example only):
{{
  "hypotheses": [
    {{
      "id": "h1",
      "statement": "Connection pool saturated by slow queries",
      "supporting_evidence_ids": ["evd_abc123"],
      "confidence": 0.88,
      "rank_explanation": "DB connections near max with elevated query latency"
    }}
  ],
  "root_cause": {{
    "summary": "DB connection pool exhausted near max connections due to slow query accumulation",
    "confidence": 0.88,
    "evidence_ids": ["evd_abc123", "evd_def456"]
  }},
  "evidence_ids": ["evd_abc123", "evd_def456"],
  "missing_evidence": ["stack traces", "pool configuration"]
}}
"""


# ---- Phase 2 Multi-Perspective Prompts ----

METRICS_SPECIALIST_SYSTEM_PROMPT = """\
You are an SRE Metrics Specialist. Analyze ONLY metrics evidence to form
hypotheses about what is happening in the service.

Rules:
- ONLY use the metrics evidence provided. Do not assume log or trace data.
- Reference evidence by its evidence_id for every claim.
- If metrics alone are insufficient, reflect low confidence (<0.5) and list
  what other evidence types would help in missing_evidence.
- Produce hypotheses that a generalist can later integrate with log/trace findings.
"""

LOGS_SPECIALIST_SYSTEM_PROMPT = """\
You are an SRE Logs Specialist. Analyze ONLY log evidence to form hypotheses
about what is happening in the service.

Rules:
- ONLY use the log evidence provided. Do not assume metrics or trace data.
- Reference evidence by its evidence_id for every claim.
- If logs alone are insufficient, reflect low confidence (<0.5) and list
  what other evidence types would help in missing_evidence.
- Produce hypotheses that a generalist can later integrate with metrics/trace findings.
"""

TRACES_SPECIALIST_SYSTEM_PROMPT = """\
You are an SRE Traces Specialist. Analyze ONLY trace evidence (plus the service
topology) to form hypotheses about what is happening in the service.

Rules:
- ONLY use the trace evidence and topology provided. Do not assume metrics or log data.
- Reference evidence by its evidence_id for every claim.
- If traces alone are insufficient, reflect low confidence (<0.5) and list
  what other evidence types would help in missing_evidence.
- Produce hypotheses that a generalist can later integrate with metrics/log findings.
"""

SYNTHESIZER_SYSTEM_PROMPT = """\
You are an SRE Incident Commander. Integrate specialist diagnoses from three
perspectives (metrics, logs, traces) plus deployment, Kubernetes, and database
evidence into a final structured diagnosis.

Rules:
- Weigh each specialist's findings by their stated confidence and evidence quality.
- Resolve contradictions between specialists explicitly.
- The runbook and memory provide known patterns — match against them.
- Output valid JSON matching the DiagnosisOutput schema.
- Cite evidence_ids from ALL sources, not just one specialist.
- Rank hypotheses by integrated evidence strength across all perspectives.
"""

SPECIALIST_PROMPT_TEMPLATE = """\
Analyze the following {perspective} evidence and produce a structured diagnosis.

Service: {service_name}
Alert: {alert_name}
Severity: {severity}
Time window: {time_window}

{evidence_block}

Reason step by step from evidence to hypotheses. Cite supporting_evidence_ids.
For each hypothesis, explain its rank in rank_explanation.
Output JSON with keys: hypotheses, root_cause, evidence_ids, missing_evidence.
Each hypothesis: id, statement, supporting_evidence_ids, confidence (0-1),
rank_explanation. Root cause: summary, confidence (0-1), evidence_ids.
"""

SYNTHESIZER_PROMPT_TEMPLATE = """\
Integrate the following specialist diagnoses and evidence into a final diagnosis.

Service: {service_name}
Alert: {alert_name}
Severity: {severity}

## Metrics Specialist Output
{metrics_output}

## Logs Specialist Output
{logs_output}

## Traces Specialist Output
{traces_output}

## Additional Evidence
{additional_evidence_block}

## Runbook
{runbook_block}

## Related Incidents (Memory)
{memory_block}

Reason step by step. Weigh each specialist's hypotheses by confidence and evidence
quality. Resolve contradictions explicitly. The final root cause and hypotheses
should integrate findings across all perspectives.

Output JSON with keys: hypotheses, root_cause, evidence_ids, missing_evidence.
Each hypothesis: id, statement, supporting_evidence_ids, confidence (0-1),
rank_explanation. Root cause: summary, confidence (0-1), evidence_ids.
"""

RANK_PROMPT_TEMPLATE = """\
Rank the following hypotheses by diagnostic strength.

Hypotheses: {hypotheses_json}

Scoring factors (weighted by importance):
1. Evidence count and quality (most important)
2. Source diversity (metrics + logs + traces > single source)
3. Deployment correlation (did a recent deploy align with the incident?)
4. Runbook match (does a known runbook describe this pattern?)
5. Memory similarity (have we seen this before?)

Return ordered from most to least likely. Preserve all fields from the input
hypotheses and add: rank, evidence_count, source_diversity, deployment_correlation,
runbook_match_score, memory_similarity_score.
"""

# Allowed action types and their risk levels (from the deterministic guardrail).
# The LLM MUST choose from this list. Unknown types default to L2 and require
# human approval — use only the types below for predictable behavior.
_ALLOWED_ACTIONS = {
    "query_metrics": "L0",
    "query_logs": "L0",
    "query_traces": "L0",
    "query_git": "L0",
    "create_ticket": "L1",
    "generate_report": "L1",
    "warmup_cache": "L1",
    "adjust_connection_pool": "L1",
    "restart_pod": "L2",
    "scale_deployment": "L2",
    "restart_service": "L2",
    "pause_rollout": "L2",
    "increase_memory_limit": "L2",
    "enable_rate_limit": "L3",
    "raise_rate_limit": "L3",
    "rollback_release": "L3",
    "rollback_deployment": "L3",
    "enable_circuit_breaker": "L3",
    "switch_dns_resolver": "L3",
    "failover": "L3",
    "scale_back": "L2",
    "revert_config": "L2",
    "cancel_deployment": "L3",
}

PLAN_ACTIONS_PROMPT_TEMPLATE = """\
Based on the root cause, propose remediation actions.

Alert: {alert_name}
Root cause: {root_cause_summary} (confidence: {root_cause_confidence})

Allowed action types and their risk levels (use ONLY these types):
{allowed_actions_table}
{rejection_feedback}{verify_feedback}{degraded_feedback}{snapshot_context}

For each action specify:
- type: one of the allowed types above
- target: the service or resource to act on
- params: key-value parameters for the action
- reason: why this action addresses the root cause
- risk_hint: L0-L4 risk level (use the levels from the table above)
- rollback_plan: mitigation, verification, rollback, or escalation plan; for
  bounded irreversible restarts, describe monitoring/escalation rather than undo

Rules:
- Prefer lower-risk actions first (L0/L1 before L2/L3)
- restart_pod and restart_service are bounded irreversible rolling restarts:
  use them only when evidence supports a restart, and do not claim they can be
  rolled back or fully restored
- rollback_plan for restart actions must describe monitoring/verification or
  follow-up escalation, not a guaranteed undo
- pause_rollout pauses a Deployment rollout by setting spec.paused=true; use it
  only for an unsafe in-progress rollout and do not claim it resumes rollout
- scale_deployment means changing Deployment replicas only; params must include replicas
- scale_deployment/scale_back are replica-count changes; use the pre-action
  snapshot when planning scale_back after degradation
- rollback_release/rollback_deployment are L3 deployment rollback actions and
  require explicit second confirmation
- Use increase_memory_limit for memory limit changes; do not encode CPU or
  memory limits in scale_deployment
- DB diagnostics and DB verify gates are read-only; never propose database
  writes, table changes, session kills, or cache flushes
- Do not put verification policy in params; required verify gates come from the
  deterministic capability registry
- L3 actions require secondary confirmation
- Never propose L4 actions (delete_data, truncate_table, flush_cache, modify_database)
- Every action must have a rollback_plan unless it is read-only (L0)
{rejection_feedback_rules}{verify_feedback_rules}{degraded_rules}

Return a JSON array of action objects.
"""

REPORT_PROMPT_TEMPLATE = """\
Generate a post-incident report.

Incident: {incident_id}
Service: {service_name}
Root cause: {root_cause_summary}
Actions: {actions_summary}
Evidence: {evidence_summary}

Include: root_cause, impact, timeline, actions, follow_ups.
The timeline should list key events in chronological order with timestamps.
Actions should include what was executed and what was proposed but rejected.
Follow-ups should be actionable items for the team.

Output JSON with keys: root_cause, impact, timeline (array of {{time, event}}),
actions (array of {{type, target, status, reason}}), follow_ups (array of strings).
"""

SUMMARIZATION_PROMPT = """\
Summarize the following content concisely. Preserve key facts, evidence IDs,
error counts, and anomalies. Note how many items were omitted (e.g. "12 log
entries omitted; retained 8 with errors").

Output format: a single paragraph of prose (not JSON). Maximum 300 words.

Content:
{content}
"""


def allowed_actions_table() -> str:
    """Format the allowed action types as a markdown table for prompts."""
    lines = ["| Action Type | Risk Level |", "|---|---|"]
    for action_type, risk in _ALLOWED_ACTIONS.items():
        lines.append(f"| {action_type} | {risk} |")
    return "\n".join(lines)
