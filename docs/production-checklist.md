# Production Readiness Checklist

Checklist for deploying sre-agent to production. All P0 items must pass before enabling scheduled discovery or Alertmanager poll.

## P0 — Blockers

| # | Check | Owner | Method | Status |
|---|-------|-------|--------|--------|
| 1 | `APP_ENV=production` is set | infra | env var check | ⬜ |
| 2 | `LLM_PROVIDER=disabled` confirmed | SRE | settings smoke test | ⬜ |
| 3 | `EXECUTOR_BACKEND=fixture` confirmed | SRE | settings smoke test | ⬜ |
| 4 | No raw secrets in DB/audit/log | security | DB scan + audit sample | ⬜ |
| 5 | Backend URL safety validator active | SRE | attempt unsafe URL in override API | ⬜ |
| 6 | PostgreSQL migration upgrade/downgrade tested | SRE | `alembic upgrade head && alembic downgrade -1` | ⬜ |
| 7 | Redis lock works with real Redis | SRE | discovery rerun with Redis lock | ⬜ |
| 8 | DisabledLLM smoke: diagnosis completes without network | SRE | alert → diagnosis with `APP_ENV=production` | ⬜ |
| 9 | API key bootstrap completed | security | create operator keys, disable bootstrap seed | ⬜ |
| 10 | `BACKEND_URL_ALLOWLIST` configured | infra | DNS patterns for cluster services | ⬜ |

## P1 — Strongly Recommended

| # | Check | Owner | Method | Status |
|---|-------|-------|--------|--------|
| 11 | Alertmanager poll scope validated (non-severity constraint) | SRE | `has_valid_scope()` check | ⬜ |
| 12 | Celery Beat singleton configured | infra | single Beat process + Redis lock | ⬜ |
| 13 | Worker reads published config only | SRE | verify no proposal leakage in AgentDeps | ⬜ |
| 14 | Override TTL enforcement active | SRE | create override with 31 days → expect 400 | ⬜ |
| 15 | Audit log immutable (DB trigger) | infra | attempt raw SQL UPDATE on audit_logs | ⬜ |
| 16 | Embedding provider available or graceful degradation | SRE | approve runbook with embedding down | ⬜ |
| 17 | `api_key:admin` does not imply business write scopes | security | admin key → attempt config publish → expect 403 | ⬜ |

## P2 — Operational Excellence

| # | Check | Owner | Method | Status |
|---|-------|-------|--------|--------|
| 18 | Prometheus metrics endpoint working | infra | `GET /metrics` | ⬜ |
| 19 | Stale config warning emitted | SRE | publish config, wait > stale period | ⬜ |
| 20 | Runbook template generation deterministic | SRE | generate template → verify no LLM calls | ⬜ |
| 21 | Discovery degraded path works | SRE | disable Prometheus → run discovery → expect degraded | ⬜ |

## Rollback / Disable Switches

All P0 failures should trigger disabling these features:

```bash
# Disable scheduled discovery
export DISCOVERY_ENABLED=false
# Disable Alertmanager poll
export ALERT_SOURCE=webhook
# Force safe defaults
export APP_ENV=production
export LLM_PROVIDER=disabled
export EXECUTOR_BACKEND=fixture
```

## Related

- [final-pre-execution-checklist.md](./final-pre-execution-checklist.md) — complete release gate
- [security-boundaries.md](./security-boundaries.md) — security model
- [degraded-behavior.md](./degraded-behavior.md) — degraded path behavior
