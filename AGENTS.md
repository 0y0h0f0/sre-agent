# Agent Coding Guide

This file is for coding agents such as Codex, Claude Code, and similar tools. It tells the agent how to generate this project safely and consistently.

The source of truth is:

1. `docs/README.md`
2. The detailed current documents under `docs/`
3. `plan.md` as the high-level planning background
4. `tzplan.md` and `plans/11-roadmap/` as the **post-MVP** expansion roadmap (background only, not MVP scope)

For implementation details, prefer the more specific `docs/` files and this `AGENTS.md` over older high-level wording in `plan.md`. Do not invent a different architecture when implementation details are already specified.

`tzplan.md` / `plans/11-roadmap/` preserve roadmap background and phase completion notes. Some roadmap slices have since been implemented; treat the current code and detailed `docs/` files as the source of truth over older MVP wording. Do not implement new roadmap items that relax safety boundaries (new real write paths, RBAC/SSO expansion, model fine-tuning, real LLM as a stable CI gate) unless the user explicitly asks for that specific slice.

## Project Summary

Maintain a completed SRE Incident Response Agent for a local demo environment, with several post-MVP productionization slices already present behind explicit configuration flags.

The system receives alerts, creates incidents, runs a LangGraph diagnosis workflow through Celery, collects metrics/logs/traces/deployment/Kubernetes/database/Runbook/Memory context, produces root cause analysis, proposes actions, applies deterministic guardrails, waits for human approval for risky actions, executes through a fixture executor by default, can optionally execute a narrow set of live Kubernetes remediations when `EXECUTOR_BACKEND=live`, verifies the result, replans when needed, generates incident reports, and persists memory.

Fixed stack:

- Backend: Python 3.11+, FastAPI, Pydantic, SQLAlchemy, Alembic.
- Agent: LangGraph.
- Async jobs: Celery.
- Database: PostgreSQL.
- Vector store: pgvector.
- Queue/cache: Redis.
- Metrics: Prometheus.
- Logs: Loki.
- Trace: fixture data by default, with Jaeger/Tempo adapters.
- Deployment changes: fixture data by default, with GitHub/Argo CD read adapters.
- Kubernetes diagnostics: fixture data by default, with a live read-only adapter.
- Database diagnostics: fixture data by default, with a live read-only PostgreSQL adapter.
- Frontend: React + TypeScript + Vite.
- Tests: pytest, pytest-cov, Vitest, React Testing Library, Playwright.

Do not replace these with OpenAI Agents SDK, Dramatiq, Elasticsearch, Next.js, Streamlit, or a different orchestration framework.

## Mandatory Boundaries

- The default local/demo path is single-tenant and safe-by-default.
- Deterministic FakeLLM/demo coverage includes the original incident types:
  - database connection exhaustion
  - high 5xx after deploy
  - Redis cache avalanche
  - Pod restart loop with mock Kubernetes events
- Deterministic FakeLLM/demo coverage also includes expanded fault classes:
  - CPU throttling
  - memory leak
  - disk full
  - certificate expiry
  - DNS failure
  - message queue lag
  - rate limit triggered
  - slow API
  - error budget burn
  - P0 site outage
  - downstream timeout
- Alert ingestion accepts arbitrary alert names, but unknown FakeLLM alerts fall back to the high-5xx diagnosis path.
- Do not add new real Kubernetes write paths beyond the existing opt-in `LiveK8sExecutorBackend`.
- The default executor is `fixture`; tests, local demo, and CI must keep using fixture/mock execution.
- `EXECUTOR_BACKEND=live` is an explicit operator opt-in. In that mode, the current live executor may perform only these Kubernetes mutations after guardrails and approval:
  - rolling restart via Deployment patch for `restart_pod` / `restart_deployment` / `restart_service`
  - rolling restart via StatefulSet patch for `restart_statefulset`
  - rollout pause via Deployment patch for `pause_rollout`
  - rollout resume via Deployment patch for `resume_rollout`
  - Deployment scale patch for `scale_deployment` / `scale_back`
  - Deployment rollback subresource call for `rollback_release`
- Do not perform real cloud resource write operations.
- Do not delete data, modify application databases, truncate tables, or flush real caches.
- Live database diagnostics must remain read-only and limited to predefined SELECT queries.
- Live Kubernetes diagnostics must remain read-only and limited to describe/logs/events/rollout status/get deployment/get statefulset.
- L2 and L3 actions require human approval.
- L3 approval requires explicit second confirmation fields:
  - `risk_ack=true`
  - `confirm_action_type`
  - `confirm_target`
- L4 actions are rejected directly and must not enter approval.
- Unit tests and CI smoke flows must use FakeLLM.
- Real LLM usage is allowed only for manual full eval or manual demo, never as a stable CI gate.

## M9 Controlled Enhancement Boundaries

M9 adds AI, Web context, Tempo, Grafana, and semantic search behind explicit feature gates. All M9 capabilities are **default-off in production**.

Core M9 invariants (from `docs/superpowers/specs/m9-foragent.md` §3):

- **Default-off**: `M9_EXTENSIONS_ENABLED=false` forces all M9 sub-capabilities off. Does not disable M8-verified Jaeger.
- **Augment, not replace**: M9 never changes M0–M8 invariants. Worker still only reads published EffectiveConfigVersion. Discovery failure does not block agent start. Raw secrets never enter DB/audit/log/prompt/state.
- **LLM drafts only**: LLM can only produce `RunbookDraft(status=pending_review)` or `AmendmentDraft(status=pending_review)`. LLM never auto-approves, auto-publishes, auto-applies amendments, or auto-executes remediation.
- **Controlled external calls**: Every external call (LLM, web_search, external embedding) requires feature flag, timeout, redaction, audit/metric, error degradation, and secret leakage test.
- **Independent rollback**: Every M9 sub-capability has an independent rollback switch. Total rollback restores `PRE_M9_TRACE_BACKEND` / `PRE_M9_TRACE_ENABLED` without hardcoding jaeger or fixture.
- **Production discovery never auto-publishes**: Tempo endpoint discovery produces `requires_review` at most in production. Only published config enters the worker.

M9 execution loop: read → plan → test-first → implement → verify → document → stop (see `docs/superpowers/specs/m9-foragent.md` §17). Agent must stop and report if M8 release gate is not passed, if invariants conflict, or if secret leakage is detected.

## Read Before Coding

Before implementing or changing a module, read the matching document:

- Architecture and scope: `docs/00-overview/architecture.md`, `docs/00-overview/scope-and-boundaries.md`
- Metrics and quality gates: `docs/07-testing/testing-strategy.md`, `docs/10-operations/development-workflow.md`
- Backend/API/data model/Celery: `docs/01-backend/`
- LangGraph and guardrails: `docs/02-agent/`
- Tool layer: `docs/03-tools/tool-layer.md`
- RAG: `docs/04-rag/runbook-rag.md`
- Memory, token cache, context compression: `docs/05-memory/memory-cache-compression.md`
- Frontend: `docs/06-frontend/react-console.md`
- Tests: `docs/07-testing/testing-strategy.md`
- Deployment: `docs/08-deploy/local-demo.md`
- Evals: `docs/09-evals/evaluation.md`
- Implementation sequence: `plans/10-codegen/implementation-order.md`
- Module checklist: `plans/10-codegen/module-checklists.md`
- Documentation quality gate: `plans/10-codegen/documentation-quality-gate.md`
- Post-MVP roadmap and completion notes: `plans/11-roadmap/README.md`
- **M9 agent execution plan**: `docs/superpowers/specs/m9-foragent.md` — PR cards, invariants, test checklists, E2E smoke sequence, rollback plan

If implementation guidance conflicts, prefer the more specific document and current code. If still ambiguous, prefer the safer option: fixture executor, FakeLLM, no new real external writes, explicit schema, and stronger tests.

## Implementation Order

Follow this order unless the user explicitly asks for a different slice:

1. Project scaffolding and tool configuration.
2. Shared settings, errors, IDs, time helpers.
3. Database models, migrations, repositories.
4. Pydantic schemas.
5. FastAPI routers and services.
6. Celery app and task stubs.
7. Tool layer with fake/mockable HTTP clients.
8. Runbook RAG.
9. Memory, token cache, context budgeting, compression.
10. LangGraph state, nodes, runner, FakeLLM.
11. Guardrail, approval, checkpoint resume, mock executor.
12. React console.
13. Tests, E2E, eval runner, README/demo scripts.

Generate tests alongside each module. Do not build the full Agent first and defer tests.

## Dependency Management

- Python dependencies are managed only through root `pyproject.toml`.
- Do not generate Poetry, Pipenv, or multiple requirements files.
- Recommended local install command: `python -m pip install -e ".[dev]"`.
- If uv is available, `uv pip install -e ".[dev]"` is acceptable, but generated scripts must not require uv.
- Frontend dependencies are managed only through npm and `apps/web/package.json`.
- Do not generate pnpm, yarn, or bun lockfiles.
- Required npm scripts: `test:coverage`, `test:e2e`.

## Target Repository Structure

Use this structure unless a later implementation document changes it:

```text
apps/
  api/
    main.py
    dependencies.py
    routers/
    schemas/
    services/
  worker/
    celery_app.py
    tasks.py
    main.py
  web/
    package.json
    src/
packages/
  agent/
  common/
  db/
  evals/
  memory/
  rag/
  tools/
demo/
  alerts/
  demo_service/
  faults/
  runbooks/
deploy/
tests/
  unit/
  integration/
  contract/
  e2e/
```

## Backend Rules

- Router functions should be thin.
- Business logic belongs in services.
- Database reads/writes belong in repositories.
- Pydantic schemas and SQLAlchemy models must be separate.
- Public IDs use prefixes such as `inc_`, `run_`, `tool_`, `act_`, `apv_`, `rpt_`, `chk_`, `mem_`.
- All times use timezone-aware UTC datetimes.
- All write APIs must support `X-Request-Id`; if missing, generate one and return it.
- Error responses must use:

```json
{
  "error": {
    "code": "VALIDATION_ERROR",
    "message": "service is required",
    "request_id": "req_123",
    "details": {}
  }
}
```

## API Requirements

Implement API contracts from `docs/01-backend/api-reference.md` and preserve compatibility with `plans/01-backend/api-contract.md` where applicable.

Core endpoints:

- `GET /healthz`
- `GET /readyz`
- `POST /api/alerts`
- `GET /api/incidents`
- `GET /api/incidents/{incident_id}`
- `POST /api/incidents/{incident_id}/diagnose`
- `GET /api/incidents/{incident_id}/runs`
- `GET /api/agent-runs/{agent_run_id}`
- `GET /api/approvals`
- `GET /api/incidents/{incident_id}/approvals`
- `POST /api/approvals/{approval_id}/approve`
- `POST /api/approvals/{approval_id}/reject`
- `GET /api/actions/{action_id}`
- `POST /api/actions/{action_id}/execute`
- `POST /api/runbooks/ingest`
- `GET /api/runbooks/search`
- `GET /api/incidents/{incident_id}/report`
- `POST /api/incidents/{incident_id}/report/regenerate`

Additional implemented API surfaces include incident NFA/feedback/correlation/audit endpoints, runbook drafts and versions, approval batch/email-token flows, comments and evidence annotations, approval groups, API keys, eval runs, shadow evals, metrics, and WebSocket node updates.

Important behavior:

- `POST /api/alerts` must create incident and agent run, then enqueue Celery. It must not run LangGraph inline.
- `fingerprint` must deduplicate open incidents.
- `GET /api/approvals?status=waiting` must support the React approval page.
- L3 approval must validate `risk_ack`, `confirm_action_type`, and `confirm_target`.
- Report regeneration must create a new report version and must not overwrite previous report versions.

## Data Model Rules

Use `docs/01-backend/data-model.md`.

Important requirements:

- `runbook_chunks.embedding` is `vector(512)` in the current schema; FakeEmbeddingProvider and BGE-ZH both output 512-dimensional vectors.
- `memory_items.embedding` is `vector(512) nullable` in the current schema.
- FakeEmbedding must be deterministic.
- Do not use random vectors in tests.
- `agent_runs.state` is only a display/debug snapshot.
- LangGraph checkpoint persistence must use PostgreSQL checkpointer, not a hand-rolled JSON state replacement.
- Business tables store checkpoint pointers:
  - `checkpoint_thread_id`
  - `checkpoint_ns`
  - `latest_checkpoint_id`

## LangGraph Rules

Use `langgraph.checkpoint.postgres.PostgresSaver` for checkpoint persistence.

Runtime config:

```python
config = {
    "configurable": {
        "thread_id": agent_run_id,
        "checkpoint_ns": "",
    }
}
```

Rules:

- `thread_id` is always `agent_run_id`.
- `checkpoint_ns` is the empty string for MVP.
- Approval resume must use the same config.
- Do not re-run completed dangerous actions after resume.
- LangGraph nodes must be ordinary testable Python functions.
- Nodes should receive dependencies through an injected dependency object.
- Nodes must not create database sessions directly.
- Large raw logs must not be placed directly into Agent state.

## Agent Workflow

Keep the implemented workflow aligned with `packages/agent/graph.py` and `docs/02-agent/workflow.md`:

```text
parse_alert
  -> collect_metrics
  -> collect_logs
  -> collect_traces
  -> collect_deployment
  -> collect_k8s
  -> collect_db
  -> retrieve_memory
  -> cross_incident
  -> retrieve_runbook
  -> build_context
  -> diagnose
  -> compress_context
  -> conditional:
       missing evidence and cycle budget remains -> collect_gap -> build_context
       otherwise -> rank_hypotheses
  -> rank_hypotheses
  -> plan_actions
  -> guardrail_check
  -> conditional:
       L0/L1 -> take_snapshot -> execute_action
       L2/L3 -> human_approval interrupt
       L4    -> generate_report
  -> conditional after approval:
       approved -> take_snapshot -> execute_action
       rejected -> plan_actions, bounded by replan cap
       otherwise -> generate_report
  -> verify
  -> conditional:
       resolved/unknown/max cycles -> generate_report
       improving/unchanged/degraded -> plan_actions
  -> generate_report
  -> persist_memory
  -> END
```

The `diagnose` node integrates evidence cross-validation (metrics/logs/traces/deployment signal fusion) and cascading-failure analysis (service topology). When `LLM_REASONING_ENABLED=true`, it produces a `diagnosis_rationale` via deep reasoning.

Every node should persist a node trace record with status, duration, input summary, output summary, and errors.

## Tool Layer Rules

Tools are in `packages/tools`.

Each tool must have:

- Pydantic query schema.
- Pydantic result schema.
- Timeout.
- Retry or degraded result behavior.
- Cache key.
- Audit-friendly summary.
- Unit tests with mocked HTTP/data sources.

Tools:

- `MetricsTool`: Prometheus.
- `LogsTool`: Loki.
- `TraceTool`: fixture by default; Jaeger/Tempo read adapters are implemented.
- `GitChangeTool`: fixture by default; GitHub/Argo CD read adapters are implemented.
- `K8sDiagnosticsTool`: read-only Kubernetes diagnostics (fixture or live).
- `DbDiagnosticsTool`: read-only database diagnostics (fixture or live).
- `RunbookSearchTool`: RAG wrapper.
- Executor backends:
  - `FixtureExecutorBackend`: default for tests, local demo, and CI.
  - `LiveK8sExecutorBackend`: opt-in via `EXECUTOR_BACKEND=live`; limited to restart/pause/resume/scale/rollback Kubernetes mutations after guardrails and approval.

Tool cache rules:

- Use UTC time buckets.
- metrics/logs: 1 minute bucket.
- traces: 5 minute bucket.
- git changes: 10 minute bucket.
- Normalize query schemas before hashing.

## RAG Rules

Use `docs/04-rag/runbook-rag.md`.

Runbook chunks:

- Target 300 to 600 tokens.
- Max 900 tokens.
- 80 token overlap.
- Keep title, parent title, source path, metadata.

RAG results must include:

- `chunk_id`
- `source_path`
- `title`
- `excerpt`
- `score`
- `metadata`

Diagnosis outputs must reference evidence IDs or Runbook chunk IDs. Do not allow root cause outputs without traceable evidence.

## Memory, Token Cache, and Context Compression

This project must include memory and context efficiency from the first Agent implementation.

Use `docs/05-memory/memory-cache-compression.md` and the matching implementation notes under `plans/05-memory/`.

Important distinction:

- Provider prompt cache depends on the LLM provider prefix caching behavior.
- App prompt segment cache is the system's Redis/application cache.
- Do not treat Redis cache hit rate as provider prompt cache hit rate.

Memory levels in the current implementation:

- L0 run-local memory: LangGraph state plus `memory_items` scoped to `run`.
- L1 incident memory: PostgreSQL `memory_items` scoped to `incident`.
- L2 service memory: PostgreSQL `memory_items` scoped to `service`; pgvector search when available, lexical fallback otherwise.
- L3 procedural memory: PostgreSQL `memory_items` scoped to `global` for successful lower-risk action patterns; versioned static knowledge remains in runbooks/runbook versions.

Context compression must trigger when:

- LogsTool returns more than 20 logs or more than 3000 tokens.
- evidence exceeds 80% of evidence budget.
- Runbook chunks exceed runbook budget.
- More than 3 collection nodes are complete before diagnosis.
- Approval resume would otherwise carry full old logs.
- Report generation needs full run trajectory compression.

Responsibility boundary:

- `packages/memory` must not directly instantiate or call LLM providers.
- `packages/memory` provides budgets, cache keys, compression plans, deterministic compression, schemas, and memory store.
- `packages/agent` performs LLM summarization through an injected summarizer adapter and writes the compressed result back to memory.

## Guardrail and Approval Rules

Risk levels:

- L0: read-only query, automatic (`query_metrics`, `query_logs`, `query_traces`, `query_git`).
- L1: low-risk local/system action, automatic (`create_ticket`, `generate_report`, `warmup_cache`, `adjust_connection_pool`).
- L2: service/Kubernetes operational action, approval required (`restart_pod`, `restart_deployment`, `scale_deployment`, `restart_service`, `restart_statefulset`, `pause_rollout`, `resume_rollout`, `scale_back`, `revert_config`).
- L3: rollback/rate-limit/deployment cancellation, approval plus second confirmation required (`enable_rate_limit`, `rollback_release`, `cancel_deployment`).
- L4: destructive data/cache/database action, direct reject (`delete_data`, `truncate_table`, `flush_cache`, `modify_database`).

Never trust the model to decide final execution permission. Use deterministic guardrail rules.

Unknown action types default conservatively to L2 and require approval. Forbidden keywords such as `delete`, `drop`, `truncate`, `modify_database`, and `flush` escalate the action to L4 even if the action type is otherwise safe.

L3 approval validation:

```text
risk_ack == true
confirm_action_type == action.type
confirm_target == action.target
```

## Frontend Rules

Use React + TypeScript + Vite.

Pages:

- `/incidents`
- `/incidents/:incidentId`
- `/incidents/:incidentId/report`
- `/agent-runs/:agentRunId`
- `/approvals`
- `/approvals/:approvalId`

Frontend must handle:

- loading state
- empty state
- error state
- polling for active runs
- approval conflict errors
- L3 second confirmation UI
- cache hit/token/compression display on Agent run page

Use TanStack Query for API state. Do not build a marketing landing page.

## Testing Requirements

Testing is not optional.

Backend:

```bash
pytest tests/unit tests/integration --cov=apps --cov=packages --cov-report=term-missing --cov-report=xml --cov-fail-under=80
```

Frontend:

```bash
npm run test:coverage
```

E2E:

```bash
npm run test:e2e
```

Coverage requirements:

- Backend total coverage > 80%.
- Frontend statements, branches, functions, lines > 80%.
- `packages/agent`, `packages/tools`, `packages/rag`, `packages/db` target >= 85%.
- `packages/agent/guardrails` target >= 95%.

Must test:

- fingerprint deduplication.
- Celery idempotency.
- LangGraph checkpoint resume.
- collect_gap bounded re-collection.
- post-action snapshot and verify/replan cycle.
- L2/L3 approval blocking execution.
- L3 missing second confirmation.
- L4 direct rejection.
- fixture executor remains the default.
- live executor is opt-in and limited to the supported Kubernetes mutations.
- FakeEmbedding determinism.
- context compression trigger.
- evidence IDs retained after compression.
- provider/app cache metrics separation.
- Runbook search returns source and chunk IDs.

## Eval Rules

CI smoke eval:

- Must use FakeLLM.
- Must not depend on external LLM provider.
- Must include at least 4 cases.
- Must enforce high-risk action block rate = 100%.
- Must enforce JSON output validity = 100%.

Manual full eval may use a real LLM, but those results are not stable CI gates.

## Code Quality Rules

- Prefer small modules over large monoliths.
- Keep functions testable.
- Avoid hardcoded absolute paths.
- Keep external service clients behind interfaces/adapters.
- Do not swallow exceptions silently; return degraded result or structured error.
- Keep raw logs out of LLM prompts unless compressed.
- Keep prompt text stable and versioned.
- Do not add broad abstractions before the local need is clear.
- Do not implement real destructive actions.

## When Unsure

Use these defaults:

- Prefer FakeLLM over real LLM.
- Prefer mock executor over real execution.
- Prefer explicit Pydantic schema over ad hoc dicts.
- Prefer deterministic fixtures over random generated data.
- Prefer safer guardrail classification.
- Prefer preserving evidence IDs over shorter but untraceable summaries.
- Prefer app segment cache metrics and mark provider cache as `unknown` if provider data is unavailable.

## Completion Criteria for Any Coding Task

For every implementation task, finish with:

1. Code implemented in the correct module.
2. Unit tests added or updated.
3. Integration/contract/E2E tests added when behavior crosses module boundaries.
4. Coverage gate still expected to pass.
5. No MVP boundary violations.
6. Relevant docs updated if behavior differs from the current docs.
