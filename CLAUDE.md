# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Current Phase: Real Backend Integration

The MVP (M1-M7) and post-MVP implementation slices are complete for the local-demo scope. The project is now in the **real backend integration** phase — extending the agent from fixture-only mode to safely connect to real Prometheus, Loki, Jaeger, Kubernetes, and Alertmanager backends for production diagnosis.

**Authoritative implementation documents:**
- `docs/superpowers/specs/2026-06-10-real-backend-integration-design.md` — design decisions
- `docs/superpowers/specs/2026-06-11-real-backend-integration-implementation-plan.md` — milestone/PR breakdown
- `sre-agent-agent-execution-plan.md` — **agent-executable task cards with hard constraints** (read this before implementing any PR)

## Agent Execution Discipline

When implementing a `PR x.y` from the execution plan:

1. **Read the PR card first**: Scope / Non-Scope / Suggested Files / Test Checklist / Acceptance Criteria / Risks / Rollback.
2. **One PR at a time** — never implement ahead of the assigned PR or across milestone boundaries.
3. **Production safety > convenience**. Default: `APP_ENV=local`, `LLM_PROVIDER=disabled` in production, `EXECUTOR_BACKEND=fixture`.
4. **Phase 0–8 does not use real LLM or web_search**. All diagnosis and runbook capabilities must work deterministically.
5. **Raw secrets never enter** DB, audit log, debug log, AgentDeps, LLM prompt, or LangGraph state.
6. **Worker only reads published EffectiveConfigVersion** — never proposals or detected_only.
7. **Backend URLs must pass safety validation** before entering EffectiveConfig or worker construction.
8. **Every PR must include tests**. Output a completion report: changes, test results, security self-check, risks, rollback, next step.
9. **If blocked**, report: what was explored, why blocked, minimal repro, suggested decision, alternative task.

Full execution rules, state machine, stop conditions, and report format are in `sre-agent-agent-execution-plan.md` §A–B.

## Commands

```bash
# Install dependencies
python -m pip install -e ".[dev]"

# Run all tests with coverage
pytest tests/unit tests/integration --cov=apps --cov=packages --cov-report=term-missing --cov-fail-under=80

# Run a single test file
pytest tests/unit/test_tools.py -v

# Run a single test
pytest tests/unit/test_tools.py::test_metrics_tool_success -v

# Lint
ruff check apps packages tests

# Type check
mypy apps packages

# Generate migration
alembic revision --autogenerate -m "description"

# Run migrations
alembic upgrade head

# Start API (local, requires postgres + redis)
uvicorn apps.api.main:app --reload --port 8000

# Start Celery worker (local)
celery -A apps.worker.tasks:celery_app worker --loglevel=INFO

# Start frontend
cd apps/web && npm run dev

# Start full stack with Docker
docker compose up -d
```

Frontend commands (run from `apps/web/`):
```bash
npm run dev           # dev server on port 5173
npm run build         # production build (tsc + vite)
npm run test          # vitest unit tests
npm run test:coverage # vitest with coverage (requires 80%+)
npm run test:e2e      # Playwright E2E tests
```

## Architecture

This is an SRE Incident Response Agent. It receives alerts, diagnoses incidents via a LangGraph workflow on Celery, and produces root cause analysis, guarded actions, approvals, reports, evals, and a React console.

**Current scope:** MVP complete (local demo with fixtures). Real backend integration in progress — the agent is being extended to safely connect to real Prometheus, Loki, Jaeger, Kubernetes, and Alertmanager for production diagnosis, while preserving fixture/demo compatibility for local dev and CI.

### Monorepo layout

- `apps/api/` — FastAPI application: routers, Pydantic schemas, services
- `apps/worker/` — Celery app and tasks (diagnosis, discovery, alertmanager poll)
- `apps/web/` — React + TypeScript + Vite console (TanStack Query, React Router)
- `packages/` — shared library code imported by both api and worker
  - `packages/agent/` — LangGraph workflow, LLM adapters (fake/disabled/real), guardrails, nodes
  - `packages/common/` — Settings (pydantic-settings), AppError, ID helpers, time utils, backend auth
  - `packages/db/` — SQLAlchemy models, repositories, session factory
  - `packages/discovery/` — **(new)** Prometheus/Loki/Jaeger/K8s/Alertmanager discovery, automation policy, config merge, runbook templates
  - `packages/tools/` — Tool client layer (Metrics, Logs, Traces, K8s, DB, GitChanges, executor) with caching
  - `packages/rag/` — Runbook RAG, ingestion, embedding
  - `packages/memory/` — Memory store, token cache, context compression
- `demo/` — demo alert fixtures, mock service, fault data
- `deploy/` — Docker Compose configs (Prometheus, Loki, Grafana, OTel collector)
- `migrations/` — Alembic migrations
- `docs/` — current reader-facing documentation and architecture references
  - `docs/superpowers/specs/` — design and implementation plan documents
- `plans/` — original implementation specs, codegen constraints, and roadmap completion notes
- `AGENTS.md` — detailed coding guide with constraints

### Layered architecture (apps/api)

```
router → service → repository (db)
           ↓
     enqueue Celery task
```

Routers are thin (validation + service call). Services contain business logic. Repositories handle all database reads/writes. Pydantic schemas and SQLAlchemy models are kept separate.

### Settings

`packages/common/settings.py` — all configuration via `pydantic-settings`, reads from env vars / `.env`. Key settings:

**Environment & safety:**
- `APP_ENV` — `local` (default) or `production`. Production enables safety defaults.
- `LLM_PROVIDER` — `fake` in local, `disabled` in production (defaults). Phase 0–8 does not call real LLMs.
- `AUTOMATION_LEVEL` — `off` | `propose` | `supervised` (default) | `autopilot`
- `DISCOVERY_ENABLED`, `DISCOVERY_APPLY_MODE` — control automatic backend discovery

**LLM & reasoning (for manual eval only, not CI):**
- `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` / `LLM_MAX_TOKENS` / `LLM_TEMPERATURE`
- `LLM_REASONING_ENABLED` / `LLM_REASONING_EFFORT` / `LLM_REASONING_NODES`

**Infrastructure:**
- `DATABASE_URL`, `REDIS_URL`, `CELERY_BROKER_URL`, `CELERY_RESULT_BACKEND`
- `TOOL_TIMEOUT_SECONDS` — default 2.0s
- `CELERY_TASK_ALWAYS_EAGER` — set to `True` for synchronous tests

**Real backend integration (new):**
- `ALERT_SOURCE` — `webhook` (default) | `poll` | `both` | `none`
- `ALERT_POLL_*` — poll interval, filters, allowlist, lock TTL
- `BACKEND_URL_ALLOWLIST` — host patterns for allowed internal service DNS
- `RUNBOOK_TEMPLATE_GENERATION_ENABLED` / `RUNBOOK_LLM_GENERATION_ENABLED` / `RUNBOOK_WEB_SEARCH_ENABLED`
- `BackendAuthConfig` — per-backend auth (bearer token, basic, mTLS) with secret references

### Database

PostgreSQL with pgvector extension. Models use prefixed public IDs (`inc_`, `run_`, `tool_`, `act_`, `apv_`, `rpt_`, `chk_`, `mem_`, `evd_`, `nd_`, `eval_`, `req_`, `key_`). All times are timezone-aware UTC.

Key model relationships:
- `Incident` has many `AgentRun`, `EvidenceItem`, `Action`
- `AgentRun` has many `AgentRunNode`
- `IncidentReport` uses unique constraint on `(incident_id, version)` — regeneration creates new versions
- `RunbookChunk` has `vector(512)` embedding column, `MemoryItem` has `vector(512) nullable`
- Fingerprint deduplication is enforced at the DB level for open incidents
- **(new)** `DiscoveryRun` → `DiscoveryProposal` → `EffectiveConfigVersion` chain
- **(new)** `DiscoveryOverride` with mandatory `expires_at`, active = `revoked_at IS NULL AND expires_at > now`
- **(new)** `AlertPollCursor` for poll dedup and cursor state
- **(new)** `ApiKey` extended with `roles` and `scopes`

### Key design constraints

- **Local by default**: `APP_ENV=local` keeps FakeLLM, fixture backends, localhost defaults for demo/CI.
- **Production safe**: `APP_ENV=production` defaults `LLM_PROVIDER=disabled`, `EXECUTOR_BACKEND=fixture`. No hidden localhost fallback.
- **Phase 0–8 deterministic**: All diagnosis, runbook template, and feedback use deterministic methods. Real LLM and web_search are gated behind explicit flags.
- **Executor backends**: Fixture executor is the default. `LiveK8sExecutorBackend` is opt-in via `EXECUTOR_BACKEND=live`, limited to restart/scale/rollback K8s mutations after guardrails and approval.
- **Risk levels**: L0 read-only (auto), L1 low-risk write (auto), L2 restart/scale (approval), L3 rollback/rate-limit (approval + second confirmation), L4 destructive (hard reject).
- **L3 approval requires** `risk_ack=true`, `confirm_action_type`, `confirm_target`.
- **POST /api/alerts** creates incident + agent run, then enqueues Celery task — never runs LangGraph inline.
- **Fingerprint** deduplicates open incidents. Poll and webhook must produce identical fingerprints for the same alert.
- **Alertmanager poll**: Uses `source=alertmanager` + `labels.ingest_mode=poll` (no new enum). Poll scope must have a non-severity constraint. Conservative resolved inference.
- **Tests must use FakeLLM** and deterministic fixtures — no random vectors.
- **Tool cache** uses UTC time buckets (metrics/logs: 1min, traces: 5min, git: 10min).
- **Error envelope** — all API errors return `{"error": {"code", "message", "request_id", "details"}}`.
- **X-Request-Id** — middleware generates one if missing, returned in response headers.
- **Operator API key** has `roles`/`scopes`. Config write requires `config:write`, discovery rerun requires `discovery:write`. `api_key:admin` manages keys only, does not imply business write scopes.
- **Manual config wins**: `env > active override > profile > published EffectiveConfigVersion > safe default`. Discovery only fills gaps, never overrides explicit config.
- **Worker reads only published config** — never unpublished proposals or detected_only backends.
- **Backend URL safety**: All URLs pass `BackendUrlSafetyValidator` before publish/override/worker use. Production rejects localhost, link-local, metadata endpoints unless explicitly allowlisted.
- **Raw secrets** use `env:VAR_NAME` references in Phase 0–8. Never stored in DB, audit, log, AgentDeps, or LLM prompt/state.
- **Audit log immutable**: No update/delete. Prefer DB trigger enforcement over ORM-only guards.
- **Discovery failure is not agent failure**: Degraded backends produce `UnavailableTool`, not crashes.
- **Override must expire**: All overrides require `expires_at`. Expired/revoked overrides do not participate in EffectiveConfig merge.
- **Regenerate creates new draft**: Runbook regenerate never overwrites the previous draft.
- **LangGraph checkpointing** uses `PostgresSaver` with `thread_id=agent_run_id`, `checkpoint_ns=""`.
- **Token cache separation** — provider cache metrics are distinct from app-level Redis cache metrics.
- **Context compression** triggers when logs > 20 entries or > 3000 tokens, evidence exceeds 80% budget, or runbook chunks exceed budget.
- **Evidence cross-validation** — `diagnose` node fuses metrics/logs/traces/deployment signals with evidence weighting.
- **Cascading-failure analysis** — service dependency graph with propagation analysis from `packages/agent/topology.py`.
- **K8s client lazy loading**: `kubernetes` package imported only on first `LiveK8sBackend.fetch()` call.
- **Celery Beat**: Separate process in production; same-process acceptable for local/CI.

## Implementation Phase Documents

When implementing real backend integration PRs:

| Document | Purpose |
|----------|---------|
| `sre-agent-agent-execution-plan.md` | Agent-executable PR task cards, global hard constraints, state machine, report format |
| `docs/superpowers/specs/2026-06-11-real-backend-integration-implementation-plan.md` | Milestone overview, dependency graph, risk register, parallelization plan |
| `docs/superpowers/specs/2026-06-10-real-backend-integration-design.md` | Design rationale, data models, protocol contracts, algorithm details |
| `AGENTS.md` | Detailed coding standards, stack constraints, per-module rules |
