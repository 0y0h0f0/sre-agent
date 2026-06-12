# M9 Rollout Plan

## Overview

M9 adds AI, Web context, Tempo, Grafana, and semantic search capabilities to the SRE agent. All capabilities are **default-off in production** behind `M9_EXTENSIONS_ENABLED`.

## Batch Schedule

| Batch | PRs | Content | Rollout Goal |
|-------|-----|---------|--------------|
| M9A | 9.1, 9.2, 9.3 | Feature gate + LLM draft + LLM diff | Controlled LLM assistance |
| M9B | 9.4 | web_search safety wrapper | Runbook enrichment with safety |
| M9C | 9.5, 9.6, 9.7 | Tempo + Grafana | Extended observability ingest |
| M9D | 9.8, 9.9, 9.10 | Semantic search + external embedding + E2E/docs | Production readiness |

## Feature Flags

All M9 features are gated individually. Each can be rolled back independently.

| Flag | Default | Purpose |
|------|---------|---------|
| `M9_EXTENSIONS_ENABLED` | `false` | Global gate — forces all M9 off when `false` |
| `RUNBOOK_LLM_GENERATION_ENABLED` | `false` | LLM-powered runbook draft generation |
| `LLM_INCIDENT_DIFF_ENABLED` | `false` | LLM incident vs runbook diff analysis |
| `RUNBOOK_WEB_SEARCH_ENABLED` | `false` | Web search for runbook enrichment |
| `TRACE_ENABLED` | `false` | Trace tool activation |
| `TRACE_BACKEND` | `disabled` | Trace backend: `disabled`, `fixture`, `jaeger`, `tempo` |
| `TEMPO_DISCOVERY_ENABLED` | `false` | Tempo endpoint auto-discovery |
| `GRAFANA_ALERT_INGEST_ENABLED` | `false` | Grafana unified alerting webhook ingest |
| `SEMANTIC_RUNBOOK_SEARCH_ENABLED` | `false` | Semantic (vector) runbook search |
| `EMBEDDING_PROVIDER` | `disabled` | Embedding backend: `disabled`, `bge_zh`, `external` |
| `EXTERNAL_EMBEDDING_PROVIDER_ENABLED` | `false` | External embedding provider |
| `LLM_EXTERNAL_PROVIDER_ALLOWED` | `false` | Double opt-in for external cloud LLM |

## Rollback

### Individual Feature Rollback

Set the specific feature flag to `false`. Example:

```env
RUNBOOK_LLM_GENERATION_ENABLED=false
```

### Total M9 Rollback

Set the global gate to `false`:

```env
M9_EXTENSIONS_ENABLED=false
```

For trace backend rollback, restore pre-M9 state:

```env
TRACE_BACKEND=${PRE_M9_TRACE_BACKEND}
TRACE_ENABLED=${PRE_M9_TRACE_ENABLED}
```

**Pre-M9 state must be recorded before M9 rollout:**

```env
PRE_M9_TRACE_BACKEND=<pre-M9 trace_backend value>
PRE_M9_TRACE_ENABLED=<pre-M9 trace_enabled value>
```

### Rollback Validation

Before total rollback, verify:
- `PRE_M9_TRACE_BACKEND` is non-empty and one of `disabled|jaeger|tempo`
- `PRE_M9_TRACE_ENABLED` is a valid boolean string
- Never fallback to `fixture` in production
- Never hardcode `jaeger`

If pre-M9 state is unknown (new environment):

```env
TRACE_ENABLED=false
TRACE_BACKEND=disabled
```

## Conflict Handling

When `M9_EXTENSIONS_ENABLED=false` but a sub-feature is `true`:

- The sub-feature is **not enabled**
- A **warning** is logged at startup
- The `agentp_m9_feature_flag_conflict_total{feature="..."}` metric is incremented
- Service continues normally — no fatal error

## Metrics

| Metric | Labels | Purpose |
|--------|--------|---------|
| `agentp_m9_feature_flag_conflict_total` | `feature` | Count of feature flag conflicts |
| `agentp_m9_feature_enabled` | `feature` | M9 feature enabled gauge (per PR) |

## PR 9.1 Deliverables

- [x] `packages/common/settings.py` — M9 settings fields + trace backend validation
- [x] `packages/common/feature_flags.py` — Feature flag resolution with conflict detection
- [x] `packages/common/metrics.py` — `m9_feature_flag_conflict_total` counter
- [x] `apps/api/dependencies.py` — M9 permission scope constants
- [x] `tests/unit/test_m9_feature_flags.py`
- [x] `tests/unit/test_trace_backend_settings.py`
- [x] `docs/m9-rollout.md` — this document
- [x] `.env.example` — M9 default settings

## Next Steps

After PR 9.1 is verified and merged:

1. **PR 9.2**: LLM Runbook Draft Generation
2. **PR 9.3**: LLM Incident Diff Analysis
3. Continue through batch M9A → M9B → M9C → M9D
