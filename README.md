# SRE Incident Response Agent

Feature-complete local-demo system that receives alerts, diagnoses incidents via a LangGraph workflow on Celery, and produces root cause analysis, guarded mock actions, approvals, reports, evals, and a React console.

The project is complete for the documented local-demo scope. Optional real-provider and real-read-backend adapters are present where documented, but the safety boundary remains unchanged: no real production Kubernetes writes, no real cloud writes, no destructive database/cache operations, and CI/smoke flows stay deterministic with FakeLLM and mock execution.

## Quick Start

```bash
# Start full stack
docker compose up -d

# Install dev dependencies
python -m pip install -e ".[dev]"

# Start API (local, requires postgres + redis)
uvicorn apps.api.main:app --reload --port 8000

# Start Celery worker (local)
celery -A apps.worker.tasks:celery_app worker --loglevel=INFO

# Start frontend
cd apps/web && npm run dev
```

## Testing

```bash
# Backend tests with coverage (requires >=80%)
pytest tests/unit tests/integration --cov=apps --cov=packages --cov-report=term-missing --cov-fail-under=80

# Single test
pytest tests/unit/test_tools.py::test_metrics_tool_success -v

# Lint
ruff check apps packages tests

# Type check
mypy apps packages

# Frontend
cd apps/web
npm run test              # vitest unit tests
npm run test:coverage     # vitest with coverage (requires 80%+)
npm run test:e2e          # Playwright E2E tests
npm run build             # production build
```

## Architecture

```
Alertmanager / Mock Alert
        |
        v
FastAPI POST /api/alerts
        |
        v
PostgreSQL incident + agent_run
        |
        v
Celery run_incident_diagnosis
        |
        v
LangGraph workflow
        |
        +--> MetricsTool -> Prometheus
        +--> LogsTool    -> Loki
        +--> TraceTool   -> fixture / Jaeger / Tempo read backend
        +--> GitTool     -> fixture / GitHub / Argo CD read backend
        +--> K8sTool     -> fixture / read-only live diagnostics
        +--> DbTool      -> fixture / read-only SQL diagnostics
        +--> RAG         -> runbook chunks + pgvector + BM25
        +--> Memory      -> run-local / incident / service memory
        |
        v
Diagnosis + Evidence + Actions
        |
        +--> L0/L1 auto execute
        +--> L2/L3 wait approval
        +--> L4 reject directly
        |
        v
Mock execution + Incident Report + UI + Eval metrics
```

## Directory Structure

```
apps/
  api/          FastAPI routers, schemas, services
  worker/       Celery app and tasks
  web/          React + TypeScript + Vite console
packages/
  agent/        LangGraph workflow, nodes, guardrails
  common/       Settings, errors, ID helpers, time utils
  db/           SQLAlchemy models, repositories
  evals/        Eval datasets, runner, metrics
  memory/       Token cache, context budget, compression
  rag/          Runbook ingest, split, retrieve, rerank
  tools/        Prometheus, Loki, Trace, Git, K8s, DB, Action tools
demo/
  alerts/       Demo alert fixtures (4 incident types)
  demo_service/ Mock service with fault injection
  faults/       Trace and Git change fixtures
  runbooks/     Markdown runbook documents
deploy/         Docker Compose configs (Prometheus, Loki, Grafana, OTel)
migrations/     Alembic migrations
tests/
  unit/         Unit tests
  integration/  Integration tests
  contract/     API contract tests
docs/           Current reader-facing documentation
plans/          Original implementation specs and roadmap background
  11-roadmap/   Post-MVP expansion phases and completion notes
```

## Tech Stack

- **Backend**: Python 3.11+, FastAPI, Pydantic, SQLAlchemy, Alembic
- **Agent**: LangGraph
- **Async Jobs**: Celery
- **Database**: PostgreSQL + pgvector
- **Cache/Queue**: Redis
- **Metrics**: Prometheus
- **Logs**: Loki
- **Trace**: OpenTelemetry demo data / fixture and optional read backends
- **Frontend**: React + TypeScript + Vite, TanStack Query
- **Tests**: pytest, pytest-cov, Vitest, Playwright

## MVP Incident Types

1. Database connection exhaustion
2. High 5xx after deploy
3. Redis cache avalanche
4. Pod restart loop (mock Kubernetes events)

## Risk Levels

| Level | Example | Policy |
|-------|---------|--------|
| L0 | Query metrics, logs, traces | Auto execute |
| L1 | Create ticket, generate report | Auto execute |
| L2 | Restart pod, scale deployment | Human approval |
| L3 | Rollback, rate-limit change | Approval + second confirmation |
| L4 | Delete data, truncate table | Direct reject |

## API

See `docs/01-backend/api-reference.md` for the current API reference.

Core endpoints:
- `GET /healthz` / `GET /readyz`
- `POST /api/alerts`
- `GET /api/incidents` / `GET /api/incidents/{id}`
- `POST /api/incidents/{id}/diagnose`
- `GET /api/incidents/{id}/runs`
- `GET /api/agent-runs/{id}`
- `GET /api/approvals` / `POST /api/approvals/{id}/approve|reject`
- `POST /api/runbooks/ingest` / `GET /api/runbooks/search`
- `GET /api/incidents/{id}/report` / `POST /api/incidents/{id}/report/regenerate`


## Eval

Run the local evaluation suites with:

```bash
python -m packages.evals.runner --suite smoke
python -m packages.evals.runner --suite full --output reports/eval-full.json
```

## Real Email Smoke Test

Fill the SMTP placeholders in `.env`, then enable the guarded manual test:

```bash
# First check whether the SMTP host/port is reachable.
RUN_REAL_EMAIL_TEST=true pytest tests/manual/test_smtp_connectivity.py -q

# Then send exactly one smoke-test email.
RUN_REAL_EMAIL_TEST=true pytest tests/manual/test_real_email_delivery.py -q
```

The send test sends exactly one email to `SRE_EMAIL_LIST`. Regular unit, integration, and CI runs skip these manual tests by default.

## Project Status

M1-M7 (MVP) and the documented post-MVP implementation slices are complete for this repository's local-demo scope. Current operating documentation lives in `docs/`; original planning background and phase notes live in `plans/`.

The delivered phase themes are:

| Phase | Delivered theme |
|-------|-----------------|
| 1 | Intelligent diagnosis upgrade: provider factory, layered reasoning, evidence validation |
| 2 | Tool layer productionization: configurable read backends, K8s/DB diagnostics, expanded fault catalog |
| 3 | Alert sources and email notifications |
| 4 | Runbook RAG enhancement: hybrid retrieval, reranker, drafts and versions |
| 5 | Memory and continuous learning: feedback, correlations, cache/compression metrics |
| 6 | Collaboration and approval workflow |
| 7 | Ops and engineering: API keys, metrics, worker health, eval/shadow paths |
| 8 | Frontend enhancement: completed React console flows |

See `docs/README.md` for the documentation center, `plans/11-roadmap/README.md` for phase-level completion notes, and `study.md` for the guided onboarding path.
