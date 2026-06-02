# SRE Incident Response Agent

Local-demo system that receives alerts, diagnoses incidents via a LangGraph workflow on Celery, and produces root cause analysis with mock actions.

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
        +--> TraceTool   -> OTel mock/demo data
        +--> GitTool     -> demo git changes
        +--> RAG         -> pgvector runbook chunks
        |
        v
Diagnosis + Evidence + Actions
        |
        +--> L0/L1 auto execute
        +--> L2/L3 wait approval
        +--> L4 reject directly
        |
        v
Incident Report + UI display
```

## Directory Structure

```
  api/          FastAPI routers, schemas, services
apps/
  worker/       Celery app and tasks
  web/          React + TypeScript + Vite console
packages/
  agent/        LangGraph workflow, nodes, guardrails
  common/       Settings, errors, ID helpers, time utils
  db/           SQLAlchemy models, repositories
  evals/        Eval datasets, runner, metrics
  memory/       Token cache, context budget, compression
  rag/          Runbook ingest, split, retrieve, rerank
  tools/        Prometheus, Loki, Trace, Git, Action tools
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
doc/            Detailed implementation specs
  11-roadmap/   Post-MVP expansion plan (Phase 1-8)
```

## Tech Stack

- **Backend**: Python 3.11+, FastAPI, Pydantic, SQLAlchemy, Alembic
- **Agent**: LangGraph
- **Async Jobs**: Celery
- **Database**: PostgreSQL + pgvector
- **Cache/Queue**: Redis
- **Metrics**: Prometheus
- **Logs**: Loki
- **Trace**: OpenTelemetry demo data / mock
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

See `doc/01-backend/api-contract.md` for full API contract.

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

## Roadmap

M1-M7 (MVP) are complete. The post-MVP expansion plan is tracked in `doc/11-roadmap/`
(source: `tzplan.md`), covering eight phases from "demo" to "production-grade":

| Phase | Theme |
|-------|-------|
| 1 | Intelligent diagnosis upgrade (real LLM provider factory, layered reasoning) |
| 2 | Tool layer productionization (real Trace/Git/K8s/DB backends) |
| 3 | Alert sources & email notifications (closed loop) |
| 4 | Runbook RAG enhancement (hybrid retrieval, reranker) |
| 5 | Memory & continuous learning |
| 6 | Collaboration & approval workflow |
| 7 | Ops & engineering (RBAC, observability, HA) |
| 8 | Frontend enhancement (realtime progress, visualization, mobile) |

See `doc/11-roadmap/README.md` for priorities, milestones, and risk tracking.
