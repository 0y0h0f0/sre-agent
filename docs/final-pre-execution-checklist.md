# Final Pre-Execution Release Gate

**Date:** 2026-06-12
**Status:** Pre-release verification
**Applies to:** Phase 0–8 completion → production deployment

This checklist is the final blocking gate before enabling production scheduled discovery and Alertmanager poll. All P0 items must pass. P1 items must have documented mitigations.

## P0 — Must Pass (Blocking)

| # | Check | Verification Environment | Steps | Expected | Result |
|---|-------|--------------------------|-------|----------|--------|
| G1 | Backend URL safety — metadata IP rejected | staging | POST override with `http://169.254.169.254/` | 400 rejected | ⬜ |
| G2 | Backend URL safety — localhost rejected in production | staging | `APP_ENV=production`, attempt localhost override | 400 rejected | ⬜ |
| G3 | DisabledLLM smoke — diagnosis completes | staging | Create alert → run diagnosis with `LLM_PROVIDER=disabled` | AgentDeps built, diagnosis complete, no errors | ⬜ |
| G4 | Redis lock — real Redis, compare-and-delete | staging | Trigger 2 concurrent discovery reruns | Second returns `locked` status | ⬜ |
| G5 | Worker does not read unpublished proposals | staging | Create proposal (pending_review), run worker | AgentDeps has UnavailableTool for that backend | ⬜ |
| G6 | Stale config still used with warning | staging | Publish config, wait past stale_after, run worker | Worker uses stale config, warning logged | ⬜ |
| G7 | Override expiry enforced | staging | Create override with expires_at in past, run worker | Expired override not used | ⬜ |
| G8 | `api_key:admin` does not grant config:write | staging | Admin key → POST /api/config/publish | 403 | ⬜ |
| G9 | Migration upgrade/downgrade round-trip | staging | `alembic upgrade head && alembic downgrade -1 && alembic upgrade head` | Success | ⬜ |
| G10 | Existing tests pass (unit + integration + E2E) | CI | `pytest tests/ -q` | All pass (excluding known xfails) | ⬜ |

## P1 — Must Have Mitigation (Non-Blocking)

| # | Check | Mitigation if Fails | Result |
|---|-------|---------------------|--------|
| G11 | Embedding provider available | Runbook search degrades to keyword-only; approved runbooks still ingested | ⬜ |
| G12 | Token/secret audit — no raw secrets in audit log | Fix redaction; rotate exposed secrets | ⬜ |
| G13 | Poll scope validated (non-severity constraint) | Block poll start if scope invalid | ⬜ |
| G14 | Override secret/auth fields rejected | Block at API level | ⬜ |
| G15 | Regenerate creates new draft (never overwrites) | Prevent regenerate if bug found | ⬜ |
| G16 | Beat singleton — duplicate detection | Deploy single Beat; Redis lock as safety net | ⬜ |

## Verification Artifacts

For each P0 gate, attach:
- Environment: (staging / CI / production-like)
- Timestamp:
- Executor:
- Evidence: (screenshot, log excerpt, API response, test output)
- Pass/Fail:
- If Fail: disable switch used / rollback performed

## Disable Switches

If any P0 gate fails, disable production features before proceeding:

```bash
# Disable all automated discovery
export DISCOVERY_ENABLED=false
# Disable Alertmanager poll  
export ALERT_SOURCE=webhook
# Force safe defaults
export APP_ENV=production
export LLM_PROVIDER=disabled
export EXECUTOR_BACKEND=fixture
```

## Post-Gate Monitoring (First 24h)

- [ ] Audit log sample: verify no raw secrets
- [ ] Prometheus: verify `sre_agent_discovery_runs_total` metric
- [ ] Alertmanager: verify poll cursor advancing
- [ ] Worker: verify `sre_agent_diagnosis_completed_total` with provider=disabled
- [ ] DB: verify no unexpected growth in audit_logs or discovery_proposals

## Related Documents

- [production-checklist.md](./production-checklist.md) — detailed production readiness
- [security-boundaries.md](./security-boundaries.md) — security model
- [backend-url-safety.md](./backend-url-safety.md) — URL safety validation
- [degraded-behavior.md](./degraded-behavior.md) — degraded path behavior
