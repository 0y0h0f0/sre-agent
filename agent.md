# Agent Coding Guide

This file is for coding agents such as Codex, Claude Code, and similar tools. It tells the agent how to generate this project safely and consistently.

The source of truth is:

1. `doc/README.md`
2. The detailed documents under `doc/`
3. `plan.md` as the high-level planning background

For implementation details, prefer the more specific `doc/` files and this `agent.md` over older high-level wording in `plan.md`. Do not invent a different architecture when implementation details are already specified.

## Project Summary

Build an SRE Incident Response Agent for a local demo environment.

The system receives alerts, creates incidents, runs a LangGraph diagnosis workflow through Celery, collects metrics/logs/traces/Git changes/Runbook context, produces root cause analysis, proposes actions, applies guardrails, waits for human approval for risky actions, executes only mock actions, and generates incident reports.

Fixed stack:

- Backend: Python 3.11+, FastAPI, Pydantic, SQLAlchemy, Alembic.
- Agent: LangGraph.
- Async jobs: Celery.
- Database: PostgreSQL.
- Vector store: pgvector.
- Queue/cache: Redis.
- Metrics: Prometheus.
- Logs: Loki.
- Trace: OpenTelemetry demo data or mock data.
- Frontend: React + TypeScript + Vite.
- Tests: pytest, pytest-cov, Vitest, React Testing Library, Playwright.

Do not replace these with OpenAI Agents SDK, Dramatiq, Elasticsearch, Next.js, Streamlit, or a different orchestration framework.

## Mandatory Boundaries

- MVP is a single-tenant local demo system.
- MVP supports exactly 4 initial incident types:
  - database connection exhaustion
  - high 5xx after deploy
  - Redis cache avalanche
  - Pod restart loop with mock Kubernetes events
- Do not perform real production Kubernetes write operations.
- Do not perform real cloud resource write operations.
- Do not delete data, modify databases, truncate tables, or flush real caches.
- All execution actions use the mock executor in MVP.
- L2 and L3 actions require human approval.
- L3 approval requires explicit second confirmation fields:
  - `risk_ack=true`
  - `confirm_action_type`
  - `confirm_target`
- L4 actions are rejected directly and must not enter approval.
- Unit tests and CI smoke flows must use FakeLLM.
- Real LLM usage is allowed only for manual full eval or manual demo, never as a stable CI gate.

## Read Before Coding

Before implementing a module, read the matching document:

- Architecture and scope: `doc/00-overview/architecture.md`, `doc/00-overview/scope.md`
- Metrics and quality gates: `doc/00-overview/engineering-metrics.md`
- Backend/API/data model/Celery: `doc/01-backend/`
- LangGraph and guardrails: `doc/02-agent/`
- Tool layer: `doc/03-tools/tools.md`
- RAG: `doc/04-rag/runbook-rag.md`
- Memory, token cache, context compression: `doc/05-memory/`
- Frontend: `doc/06-frontend/react-console.md`
- Tests: `doc/07-testing/testing-strategy.md`
- Deployment: `doc/08-deploy/demo-environment.md`
- Evals: `doc/09-evals/evaluation.md`
- Implementation sequence: `doc/10-codegen/implementation-order.md`
- Module checklist: `doc/10-codegen/module-checklists.md`
- Documentation quality gate: `doc/10-codegen/documentation-quality-gate.md`

If implementation guidance conflicts, prefer the more specific document. If still ambiguous, prefer the safer option: mock executor, FakeLLM, no real external writes, explicit schema, and stronger tests.

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

Implement API contracts from `doc/01-backend/api-contract.md`.

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

Important behavior:

- `POST /api/alerts` must create incident and agent run, then enqueue Celery. It must not run LangGraph inline.
- `fingerprint` must deduplicate open incidents.
- `GET /api/approvals?status=waiting` must support the React approval page.
- L3 approval must validate `risk_ack`, `confirm_action_type`, and `confirm_target`.
- Report regeneration must create a new report version and must not overwrite previous report versions.

## Data Model Rules

Use `doc/01-backend/data-model.md`.

Important requirements:

- `runbook_chunks.embedding` is `vector(384)` for MVP.
- `memory_items.embedding` is `vector(384) nullable` for MVP.
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

Implement the workflow from `doc/02-agent/langgraph-workflow.md`:

```text
parse_alert
  -> collect_metrics
  -> collect_logs
  -> collect_traces
  -> collect_deployment_context
  -> retrieve_memory
  -> retrieve_runbook
  -> build_context
  -> diagnose
  -> rank_hypotheses
  -> plan_actions
  -> guardrail_check
  -> conditional:
       L0/L1 -> execute_action
       L2/L3 -> human_approval interrupt
       L4    -> generate_report
  -> generate_report
```

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
- `TraceTool`: mock/OpenTelemetry demo data.
- `GitChangeTool`: demo fixture.
- `RunbookSearchTool`: RAG wrapper.
- `ActionExecutorTool`: mock executor only.

Tool cache rules:

- Use UTC time buckets.
- metrics/logs: 1 minute bucket.
- traces: 5 minute bucket.
- git changes: 10 minute bucket.
- Normalize query schemas before hashing.

## RAG Rules

Use `doc/04-rag/runbook-rag.md`.

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

Use `doc/05-memory/token-cache-and-context.md` and `doc/05-memory/memory-implementation.md`.

Important distinction:

- Provider prompt cache depends on the LLM provider prefix caching behavior.
- App prompt segment cache is the system's Redis/application cache.
- Do not treat Redis cache hit rate as provider prompt cache hit rate.

Memory levels:

- L0 run-local memory: LangGraph state + Redis short TTL.
- L1 incident memory: PostgreSQL `memory_items`.
- L2 service memory: PostgreSQL + pgvector.
- L3 procedural memory: versioned static knowledge.

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

- L0: read-only query, automatic.
- L1: low-risk write such as report/ticket, automatic.
- L2: restart pod or scale deployment, approval required.
- L3: rollback or rate-limit change, approval plus second confirmation required.
- L4: delete data, truncate table, flush cache, modify database, direct reject.

Never trust the model to decide final execution permission. Use deterministic guardrail rules.

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
- `/agent-runs/:agentRunId`
- `/approvals`
- `/incidents/:incidentId/report`

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
- L2/L3 approval blocking execution.
- L3 missing second confirmation.
- L4 direct rejection.
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

