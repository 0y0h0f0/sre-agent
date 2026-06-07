# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

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

This is a completed SRE Incident Response Agent for the documented local-demo scope. It receives alerts, diagnoses incidents via a LangGraph workflow on Celery, and produces root cause analysis, guarded mock actions, approvals, reports, evals, and a React console.

### Monorepo layout

- `apps/api/` — FastAPI application: routers, Pydantic schemas, services
- `apps/worker/` — Celery app and tasks (diagnosis workflow)
- `apps/web/` — React + TypeScript + Vite console (TanStack Query, React Router)
- `packages/` — shared library code imported by both api and worker
  - `packages/common/` — Settings (pydantic-settings), AppError types, ID helpers, time utils
  - `packages/db/` — SQLAlchemy models, repositories, session factory
  - `packages/tools/` — Tool client layer (Metrics, Logs, Traces, GitChanges) with caching
- `demo/` — demo alert fixtures, mock service, fault data
- `deploy/` — Docker Compose configs (Prometheus, Loki, Grafana, OTel collector)
- `migrations/` — Alembic migrations
- `docs/` — current reader-facing documentation and architecture references
- `plans/` — original implementation specs, codegen constraints, and roadmap completion notes
  - `plans/11-roadmap/` — post-MVP phase notes, sourced from `tzplan.md`
- `AGENTS.md` — coding guide with detailed constraints (read before implementing or changing code)

### Layered architecture (apps/api)

```
router → service → repository (db)
           ↓
     enqueue Celery task
```

Routers are thin (validation + service call). Services contain business logic. Repositories handle all database reads/writes. Pydantic schemas and SQLAlchemy models are kept separate.

### Settings

`packages/common/settings.py` — all configuration via `pydantic-settings`, reads from env vars / `.env`. Key settings:
- `DATABASE_URL`, `REDIS_URL`, `CELERY_BROKER_URL`, `CELERY_RESULT_BACKEND`
- `LLM_PROVIDER` / `EMBEDDING_PROVIDER` — must be `"fake"` for tests and local dev. `LLM_PROVIDER` selects an adapter via `packages/agent/llm/factory.py`: `fake` | `vllm` | `openai` | `deepseek` | `anthropic`
- `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` / `LLM_MAX_TOKENS` / `LLM_TEMPERATURE` / `LLM_REASONING_ENABLED` / `LLM_REASONING_EFFORT` — provider config (only used by non-fake adapters; completed Phase 1.1)
- `LLM_REASONING_NODES` — comma-separated nodes that use deep reasoning when `LLM_REASONING_ENABLED` is true (default `diagnose`; completed Phase 1.2). Gated by `packages/agent/llm/reasoning.py`; the `diagnose` node emits an auditable `diagnosis_rationale` and records LLM call metadata in `state["llm_calls"]` without persisting raw chain-of-thought
- `TOOL_TIMEOUT_SECONDS` — default 2.0s
- `CELERY_TASK_ALWAYS_EAGER` — set to `True` to run tasks synchronously in tests

### Database

PostgreSQL with pgvector extension. Models use prefixed public IDs (`inc_`, `run_`, `tool_`, `act_`, `apv_`, `rpt_`, `chk_`, `mem_`, `evd_`, `nd_`, `eval_`, `req_`). All times are timezone-aware UTC.

Key model relationships:
- `Incident` has many `AgentRun`, `EvidenceItem`, `Action`
- `AgentRun` has many `AgentRunNode`
- `IncidentReport` uses unique constraint on `(incident_id, version)` — regeneration creates new versions
- `RunbookChunk` has `vector(384)` embedding column, `MemoryItem` has `vector(384) nullable`
- Fingerprint deduplication is enforced at the DB level for open incidents

### Key design constraints

- **Mock executor only in MVP** — no real production writes, no destructive actions
- **Risk levels**: L0 read-only (auto), L1 low-risk write (auto), L2 restart/scale (approval), L3 rollback/rate-limit (approval + second confirmation), L4 destructive (hard reject)
- **L3 approval requires** `risk_ack=true`, `confirm_action_type`, `confirm_target`
- **POST /api/alerts** creates incident + agent run, then enqueues Celery task — never runs LangGraph inline
- **Fingerprint** deduplicates open incidents
- **Tests must use FakeLLM** and deterministic fixtures — no random vectors
- **Tool cache** uses UTC time buckets (metrics/logs: 1min, traces: 5min, git: 10min)
- **Error envelope** — all API errors return `{"error": {"code", "message", "request_id", "details"}}`
- **X-Request-Id** — middleware generates one if missing, returned in response headers
- **LangGraph checkpointing** uses `PostgresSaver` with `thread_id=agent_run_id`, `checkpoint_ns=""`
- **Token cache separation** — provider cache metrics are distinct from app-level Redis cache metrics
- **Context compression** triggers when logs > 20 entries or > 3000 tokens, evidence exceeds 80% budget, or runbook chunks exceed budget
- **Evidence cross-validation** (completed Phase 1.3, `packages/agent/evidence_validation.py`) — the `diagnose` node fuses metrics/logs/traces/deployment signals (weights Trace > Metrics > Logs > Git); corroboration raises root-cause confidence, conflict sets `state["needs_human_review"]`, missing sources degrade without blocking. Deployment absence is neutral, not a healthy dissent
- **Cascading-failure analysis** (completed Phase 1.4, `packages/agent/topology.py`) — service dependency graph (config `SERVICE_TOPOLOGY_PATH`/`demo/topology.json` or trace-derived); `analyze_propagation` finds the root service of a chain, `correlate_incidents` clusters co-occurring related incidents. The `diagnose` node writes `state["cascade_analysis"]` (informational; `is_cascade=False` for single-service incidents)

## Project Status And Roadmap Notes

M1-M7 (the MVP) and the documented post-MVP implementation slices are complete for this repository's local-demo scope. Current reader documentation lives in `docs/`; phase-level completion notes live in `plans/11-roadmap/` (sourced from `tzplan.md`).

Completion does not relax the safety boundaries above: keep mock-executor-only for execution, FakeLLM in tests and CI smoke, no real production K8s/cloud writes, L4 hard reject, and L3 second confirmation. Items that would loosen scope, such as production writes, model fine-tuning, or full enterprise RBAC/SSO, still require separate sign-off and must not be implemented by default.
