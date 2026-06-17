# Glossary

**Last updated:** 2026-06-14

This glossary defines terms used across the SRE Incident Response Agent documentation and codebase. Prefer these names in docs, comments, API descriptions, and PR notes.

## Core System Terms

| Term | Meaning |
|------|---------|
| Agent | The LangGraph diagnosis workflow that collects evidence, diagnoses, plans actions, applies guardrails, handles approval, executes, verifies, reports, and persists memory. |
| Incident | A normalized alert instance persisted in `incidents`, identified by `inc_`. |
| Alert | Incoming provider payload or normalized request that can create or deduplicate an incident. |
| Fingerprint | Stable deduplication key. Open incidents with the same fingerprint are deduplicated. |
| Agent run | One diagnosis workflow execution for an incident, identified by `run_`. |
| Node trace | Per-node execution record stored in `agent_run_nodes`, identified by `nd_`. |
| Tool call | Auditable record of a tool invocation, identified by `tool_`. |
| Evidence item | Traceable evidence used by diagnosis, identified by `evi_`. |
| Report | Versioned incident report, identified by `rpt_`. Report regeneration creates a new version. |
| Request ID | `X-Request-Id` value or generated `req_` ID used in responses/errors/audit correlation. |

## Architecture Terms

| Term | Meaning |
|------|---------|
| Router | Thin FastAPI endpoint layer under `apps/api/routers`. |
| Service | Business logic layer under `apps/api/services`. |
| Repository | Database access layer under `packages/db/repositories`. |
| Schema | Pydantic request/response model under `apps/api/schemas`. |
| ORM model | SQLAlchemy table mapping in `packages/db/models.py`. |
| Worker | Celery process that executes diagnosis, resume, discovery, polling, notification, and eval tasks. |
| Beat | Celery Beat scheduler for periodic tasks. |
| EffectiveConfig | Merged runtime backend config used by workers. Priority: env > active override > profile > published config > safe default. |
| Published config | `EffectiveConfigVersion(status=published)`, identified by `ecv_`. Workers select only published records. |
| Override | Time-bounded operator backend override, identified by `dov_`. Secret/auth/executor/live fields are forbidden in the general override API. |
| Discovery | Backend/service/topology/capability detection subsystem. Discovery runs use `dr_`; proposals use `dp_`. |

## Agent Workflow Terms

| Term | Meaning |
|------|---------|
| LangGraph | Graph orchestration framework used for the Agent workflow. Do not replace it with another orchestration framework. |
| Checkpoint | LangGraph persisted execution state. Current runtime uses `thread_id=agent_run_id` and `checkpoint_ns=""`. |
| GraphInterrupt | LangGraph interruption used when human approval is required. |
| Resume | Continuing the same checkpointed run after approval/rejection. Resume must not re-run completed dangerous actions. |
| Replan | Planning another action after rejection or unsatisfactory verification, bounded by caps. |
| Verification loop | Post-action check that classifies result as resolved/improving/unchanged/degraded/unknown and may replan. |
| Snapshot | Evidence captured before execution to support audit and rollback reasoning. |
| FakeLLM | Deterministic local LLM provider used by tests, CI smoke eval, and default local demo. |
| Reasoning mode | Optional deeper LLM rationale path controlled by `LLM_REASONING_ENABLED`. |
| Multi-perspective diagnosis | Optional mode that runs specialist perspectives before synthesis; default off. |

## Tool Terms

| Term | Meaning |
|------|---------|
| Tool | Testable adapter with query/result schemas, timeout, degradation, cache key, and audit-friendly summary. |
| Metrics tool | Prometheus-backed or degraded metrics collector. |
| Logs tool | Loki-backed or degraded logs collector. |
| Trace tool | Fixture/Jaeger/Tempo/degraded trace collector. |
| Git change tool | Fixture/GitHub/Argo CD deployment-change collector. |
| K8s diagnostics tool | Fixture or live read-only Kubernetes diagnostic collector. |
| DB diagnostics tool | Fixture or live read-only PostgreSQL diagnostic collector. |
| Executor backend | Action execution adapter. Default is `fixture`; live backend is explicit opt-in and limited to supported K8s mutations. |
| Degraded result | Structured fallback result that lets diagnosis continue with partial/missing evidence. |
| Cache bucket | UTC time-bucketed cache key component used to normalize tool queries. |

## Guardrail and Approval Terms

| Term | Meaning |
|------|---------|
| Guardrail | Deterministic risk classifier and policy gate. The model does not decide final execution permission. |
| L0 | Read-only automatic action. |
| L1 | Low-risk automatic local/system action. |
| L2 | Operational action requiring human approval. |
| L3 | Higher-risk action requiring approval plus second confirmation fields. |
| L4 | Destructive action directly rejected and never sent to approval. |
| Approval | Human decision record identified by `apv_`. |
| Second confirmation | L3 approval fields: `risk_ack=true`, `confirm_action_type`, and `confirm_target`. |
| Batch approval | API path for deciding multiple approvals. L3 still requires exact second-confirmation fields. |
| Stale auto-approve | Optional approval automation controlled by `APPROVAL_AUTO_APPROVE_MINUTES`; default is disabled. |

## RAG and Runbook Terms

| Term | Meaning |
|------|---------|
| Runbook | Markdown operational document with metadata, detection, evidence, decision, action, and rollback content. |
| Chunk | Searchable runbook section, identified by `chk_`. |
| Embedding | Vector representation stored for retrieval. Current core runbook/memory schema uses 512-dimensional vectors. |
| Hybrid search | Weighted keyword + vector search controlled by `RUNBOOK_HYBRID_SEARCH_ENABLED` and alpha settings. |
| Reranker | Optional second-stage ranking provider; default is deterministic fake. |
| Draft | Reviewable runbook proposal, identified by `drf_`. |
| Version | Published runbook version, identified by `ver_`. |
| Amendment | Reviewable proposed change to an existing runbook, identified by `amd_`. |
| Template draft | Deterministic generated runbook draft. |
| LLM draft | M9 LLM-generated runbook draft; it must start `pending_review`. |
| Incident diff | M9 LLM analysis comparing an incident to an approved runbook and producing amendments only. |

## Memory and Context Terms

| Term | Meaning |
|------|---------|
| Memory item | Persisted memory record identified by `mem_`. |
| L0 memory | Run-local state and run-scoped memory. |
| L1 memory | Incident-scoped memory. |
| L2 memory | Service-scoped memory. |
| L3 memory | Global/procedural memory for successful lower-risk action patterns. |
| Context budget | Token budget for evidence, runbook context, prompts, and report generation. |
| Compression event | Recorded reduction of large evidence/context. |
| App prompt segment cache | Redis/application-level cache for prompt segments. |
| Provider prompt cache | LLM provider-side cache behavior. Do not treat Redis hits as provider cache hits. |

## M9 Terms

| Term | Meaning |
|------|---------|
| M9 | Controlled enhancement set adding AI drafts, Web context, Tempo, Grafana helper, semantic search, and external embeddings behind explicit gates. |
| Global M9 gate | `M9_EXTENSIONS_ENABLED`; when false, M9 sub-feature flags resolve disabled. |
| Sub-feature flag | Individual M9 toggle such as `RUNBOOK_WEB_SEARCH_ENABLED` or `LLM_INCIDENT_DIFF_ENABLED`. |
| Feature flag conflict | A state where a sub-feature is set true while global M9 is false, or Tempo is selected while M9 is disabled. Logged and counted by metrics. |
| Web context | Redacted, URL-validated Web search result used as review evidence for runbook enrichment. |
| Native Tempo backend | `TRACE_BACKEND=tempo` using Tempo HTTP APIs. Treat as M9 rollout. |
| Tempo discovery | M9 discovery of Tempo services from K8s service data. Production status is at most `requires_review`. |
| Grafana webhook helper | Gated helper path `AlertService.ingest_grafana_alert()`. The generic alert endpoint can still normalize Grafana-shaped payloads. |
| Semantic search | M9/hybrid runbook search mode using embeddings when enabled and available. |
| External embedding provider | Optional HTTP embedding provider that must redact input and degrade to keyword fallback on failure. |
| External LLM allow | `LLM_EXTERNAL_PROVIDER_ALLOWED`, the second opt-in for cloud LLM providers. |
| Full M9 rollback | Disabling global/sub-feature flags and restoring trace settings from `PRE_M9_TRACE_BACKEND` / `PRE_M9_TRACE_ENABLED`. |

## Security Terms

| Term | Meaning |
|------|---------|
| Safe-by-default | Default local/demo/CI posture uses FakeLLM, fixture executor, fixture diagnostics, and no real external mutation. |
| Read-only diagnostics | Live K8s/DB diagnostic modes may read only predefined safe data and must not mutate external systems. |
| Live executor | Explicit opt-in executor that can perform only narrow approved K8s restart/pause/resume/scale/rollback mutations. |
| Secret reference | A pointer such as `env:VAR_NAME` used instead of storing raw secret values. |
| Redaction | Deterministic removal of tokens, passwords, private keys, internal URLs, private IPs, and similar sensitive strings. |
| SSRF protection | Backend URL validation that blocks unsafe schemes, metadata endpoints, and production localhost/private IPs unless allowlisted. |
| Audit log | Immutable record of who did what and when, identified by `adt_` for API/repository audit paths. |
| NFA | Not Actionable Alert. Repeated NFA marks create false-positive patterns identified by `nfp_`. |
| High-risk action block rate | Eval metric requiring destructive/high-risk actions to be blocked. CI smoke requires 100%. |

## Evaluation and Testing Terms

| Term | Meaning |
|------|---------|
| Smoke eval | Deterministic CI-friendly eval suite using FakeLLM. |
| Full eval | Larger manual eval suite; may use real LLM only outside stable CI gates. |
| Shadow eval | Comparative evaluation path that does not affect production diagnosis. |
| Replay | Re-running stored/eval incidents through a model/prompt path. |
| Contract test | Test that enforces API/error/schema compatibility. |
| E2E smoke | Browser or API-level flow that validates the integrated stack. |
| Coverage gate | Backend and frontend coverage thresholds documented in testing strategy. |

## Operations Terms

| Term | Meaning |
|------|---------|
| Local demo | Docker Compose stack with deterministic fixtures and demo-service. |
| Fixture backend | Default deterministic backend that reads local fixture data or returns mock execution. |
| Production checklist | Required pre-flight checks before enabling production-like operation. |
| Rollback switch | Environment/config flag that disables a capability without code rollback. |
| Degraded startup | Starting with a capability unavailable while preserving core diagnosis where safe. |
| Orphaned task | Agent run stuck in running state beyond `TASK_ORPHAN_TIMEOUT_SECONDS`. |
| Alert poll cursor | Alertmanager polling dedup/resolved-inference record keyed by filter hash and fingerprint. |

## Naming Rules

Use these names consistently:

- `fixture executor`, not mock executor, when referring to the default `EXECUTOR_BACKEND=fixture` path.
- `FakeLLM`, not fake model, when referring to the deterministic provider.
- `EffectiveConfigVersion`, not runtime config row, when referring to published worker config snapshots.
- `RunbookDraft` and `AmendmentDraft`, not generated runbook, for unreviewed AI output.
- `provider cache` and `app cache` separately; do not merge those metrics.
- `M9 sub-feature`, not plugin, for controlled enhancement flags.
