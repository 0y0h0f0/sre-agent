# M9 Rollout, Feature Gates, and Rollback

**Last updated:** 2026-06-14

M9 adds optional AI, Web, Tempo, Grafana, semantic search, and external embedding capabilities on top of the M0-M8 deterministic incident response system. These capabilities are controlled enhancements, not a replacement for the fixture/FakeLLM default path.

The safe default remains:

- `M9_EXTENSIONS_ENABLED=false`.
- `LLM_PROVIDER=fake`.
- `EXECUTOR_BACKEND=fixture`.
- `TRACE_BACKEND=fixture` in the default application settings and Compose runtime.
- No real external write path is enabled by M9.

## Non-Negotiable Invariants

| Area | Invariant |
|------|-----------|
| Global gate | `M9_EXTENSIONS_ENABLED=false` forces M9 sub-capabilities off in the feature flag resolver. Sub-feature conflicts emit logs and `agentp_m9_feature_flag_conflict_total`. |
| M8 compatibility | M9 does not disable the M8 Jaeger path. `TRACE_BACKEND=jaeger` remains a supported non-M9 trace backend. |
| Tempo | Native Tempo is an M9 trace backend. With M9 disabled, `TRACE_BACKEND=tempo` is treated as a feature flag conflict by the resolver. |
| LLM | LLM output can create only `RunbookDraft(status=pending_review)` or `AmendmentDraft(status=pending_review)`. It never approves, publishes, applies, or executes. |
| External LLM | External/cloud LLM providers require both the relevant M9 sub-feature and `LLM_EXTERNAL_PROVIDER_ALLOWED=true`. |
| Web search | Web search results are review evidence only. Production requires an allowed-domain policy. |
| External embedding | External embedding input is redacted and failure degrades to keyword/hybrid fallback. It must not block runbook ingest. |
| Discovery | Production discovery may produce review-required proposals; it must not auto-publish production config. |
| Secrets | Raw tokens, passwords, private keys, auth headers, and secret values must not enter DB, audit logs, prompts, Agent state, or metrics labels. |
| Rollback | Each M9 sub-capability has its own switch. Global rollback turns off `M9_EXTENSIONS_ENABLED` and restores trace settings from `PRE_M9_TRACE_BACKEND` / `PRE_M9_TRACE_ENABLED` when those are recorded. |

## Feature Gates

| Capability | Gate / Setting | Default | Runtime effect |
|------------|----------------|---------|----------------|
| Global M9 gate | `M9_EXTENSIONS_ENABLED` | `false` | Required by `packages.common.feature_flags` before M9 sub-features resolve to enabled. |
| LLM runbook generation | `RUNBOOK_LLM_GENERATION_ENABLED` | `false` | Enables `POST /api/runbooks/llm-generate`; creates `drf_` draft with `pending_review`. |
| LLM incident diff | `LLM_INCIDENT_DIFF_ENABLED` | `false` | Enables `POST /api/runbooks/incident-diff`; creates `amd_` amendments with `pending_review` after evidence threshold. |
| Runbook Web search | `RUNBOOK_WEB_SEARCH_ENABLED` | `false` | Enables `POST /api/runbooks/web-search`; provider defaults to `disabled`. |
| Web search provider | `RUNBOOK_WEB_SEARCH_PROVIDER` | `disabled` | `fake` is deterministic local/CI; unknown providers degrade to disabled. |
| Native Tempo backend | `TRACE_BACKEND=tempo` and `TRACE_ENABLED=true` | `fixture` / `true` | Uses `TempoTraceBackend`; treat as M9 rollout even though backend construction follows `TRACE_BACKEND`. |
| Tempo discovery | `TEMPO_DISCOVERY_ENABLED` | `false` | Allows discovery to include Tempo service endpoints. |
| Grafana alert ingest helper | `GRAFANA_ALERT_INGEST_ENABLED` | `false` | Enables `AlertService.ingest_grafana_alert()` helper. The generic `/api/alerts` schema can still normalize `source=grafana` payloads. |
| Semantic runbook search | `SEMANTIC_RUNBOOK_SEARCH_ENABLED` | `false` | Resolves semantic mode to hybrid only with a supported embedding provider. |
| External embedding provider | `EXTERNAL_EMBEDDING_PROVIDER_ENABLED` | `false` | Allows controlled external embedding jobs; current base `build_embedding_provider()` supports `fake`, `bge_zh`, and `text2vec`. |
| External LLM explicit allow | `LLM_EXTERNAL_PROVIDER_ALLOWED` | `false` | Required when `LLM_PROVIDER` is `openai`, `deepseek`, or `anthropic`. |
| Full trace rollback | `PRE_M9_TRACE_BACKEND`, `PRE_M9_TRACE_ENABLED` | empty | Operator-recorded previous trace settings used by rollback procedures. |

## Recommended Rollout Order

1. Establish a baseline with M9 off.

   ```bash
   M9_EXTENSIONS_ENABLED=false
   RUNBOOK_LLM_GENERATION_ENABLED=false
   LLM_INCIDENT_DIFF_ENABLED=false
   RUNBOOK_WEB_SEARCH_ENABLED=false
   TEMPO_DISCOVERY_ENABLED=false
   GRAFANA_ALERT_INGEST_ENABLED=false
   SEMANTIC_RUNBOOK_SEARCH_ENABLED=false
   EXTERNAL_EMBEDDING_PROVIDER_ENABLED=false
   ```

2. Record the pre-M9 trace state before enabling Tempo:

   ```bash
   PRE_M9_TRACE_BACKEND="$TRACE_BACKEND"
   PRE_M9_TRACE_ENABLED="$TRACE_ENABLED"
   ```

3. Enable the global gate alone and verify no sub-feature is active unexpectedly.

   ```bash
   M9_EXTENSIONS_ENABLED=true
   ```

4. Enable one sub-feature at a time. Prefer this order:

   - LLM runbook draft generation with FakeLLM/local LLM first.
   - LLM incident diff.
   - Web search with `RUNBOOK_WEB_SEARCH_PROVIDER=fake`.
   - Tempo backend in a staging environment.
   - Tempo discovery.
   - Grafana webhook helper.
   - Semantic search with local/fake embeddings.
   - External embedding provider only after security review.

5. Run focused tests and a manual smoke for the enabled capability.

6. Watch metrics and audit logs before enabling the next capability.

## Capability Notes

### LLM Runbook Drafts

Entry point: `POST /api/runbooks/llm-generate`

Required scopes when API key auth is on:

- `runbook:review` or `runbook:llm_generate`

Implementation path:

- Router: `apps/api/routers/runbooks.py`
- Service: `apps/api/services/runbook_service.py`
- Domain: `packages/rag/llm_runbook_generator.py`
- Prompt builder: `packages/rag/runbook_prompt_builder.py`

Behavior:

- Disabled when `M9_EXTENSIONS_ENABLED=false` or `RUNBOOK_LLM_GENERATION_ENABLED=false`.
- External providers are blocked unless `LLM_EXTERNAL_PROVIDER_ALLOWED=true`.
- Prompt inputs are redacted.
- Draft content is classified for risky action wording.
- The persisted draft is `RunbookDraft(status=pending_review, draft_type=llm_generated)`.
- Review is still performed through `POST /api/runbooks/drafts/{draft_id}/review`.

Rollback:

```bash
RUNBOOK_LLM_GENERATION_ENABLED=false
```

### LLM Incident Diff

Entry point: `POST /api/runbooks/incident-diff`

Required scopes:

- `runbook:review` and `incident:llm_diff`
- External cloud LLM providers additionally require `llm:invoke` or `ai:external`.

Behavior:

- Requires enough evidence before invoking LLM: diagnosis report, operator feedback, action results, linked approved version, or at least `MIN_INCIDENT_DIFF_EVIDENCE_REFS` evidence refs.
- Produces `AmendmentDraft(status=pending_review)` records.
- Review uses `POST /api/runbooks/amendments/{amendment_id}/review`.
- `approved` and `applied` are separate states; low-confidence notes without evidence cannot be applied.
- `applied` review requests must name exactly one target: a reviewed draft or a runbook version.

Rollback:

```bash
LLM_INCIDENT_DIFF_ENABLED=false
```

Database downgrade is not the normal rollback path for this capability. If an
operator needs to downgrade the migration, M9 amendment rows with nullable
`summary_id` must be handled first.

### Web Search for Runbook Enrichment

Entry point: `POST /api/runbooks/web-search`

Required scopes:

- `runbook:review` or `runbook:web_search`

Behavior:

- Query text is redacted before provider invocation.
- `RUNBOOK_WEB_SEARCH_PROVIDER=disabled` returns degraded.
- `RUNBOOK_WEB_SEARCH_PROVIDER=fake` returns deterministic local results.
- Production blocks Web search unless `RUNBOOK_WEB_SEARCH_ALLOWED_DOMAINS` is set.
- Returned URLs are validated and results include traceability metadata.

Rollback:

```bash
RUNBOOK_WEB_SEARCH_ENABLED=false
RUNBOOK_WEB_SEARCH_PROVIDER=disabled
```

### Native Tempo Trace Backend

Settings:

```bash
TRACE_ENABLED=true
TRACE_BACKEND=tempo
TEMPO_URL=http://localhost:3200
```

Behavior:

- `build_trace_backend()` constructs `TempoTraceBackend` when `TRACE_BACKEND=tempo`.
- Tempo supports trace-by-ID, time-range search, and TraceQL methods with capability flags.
- Fetch failures return empty/degraded trace evidence instead of crashing diagnosis.
- Rollout should still be guarded by `M9_EXTENSIONS_ENABLED=true` and monitoring of feature flag conflicts.

Rollback:

```bash
TRACE_BACKEND="${PRE_M9_TRACE_BACKEND:-fixture}"
TRACE_ENABLED="${PRE_M9_TRACE_ENABLED:-true}"
```

### Tempo Discovery

Behavior:

- `BackendEndpointDetector` includes Tempo service detection only when `is_m9_subfeature_enabled(settings, "tempo_discovery")` resolves true.
- Production-discovered URLs are `requires_review`; local discovery may mark safe endpoints `ready`.
- Unsafe Tempo URLs are rejected rather than silently accepted.

Rollback:

```bash
TEMPO_DISCOVERY_ENABLED=false
```

### Grafana Alert Ingest

There are two related but different paths:

- Generic `POST /api/alerts` accepts normalized alerts and can normalize provider-shaped payloads, including `source=grafana`.
- `AlertService.ingest_grafana_alert()` is the M9 Grafana webhook helper and is gated by `GRAFANA_ALERT_INGEST_ENABLED`.

Behavior:

- Grafana payload parsing uses stable fingerprints that exclude volatile dashboard/panel/rule/generator URL fields.
- Malformed helper payloads raise a structured failure path and increment Grafana ingest metrics.
- Duplicate fingerprints still deduplicate through the normal incident path.

Rollback:

```bash
GRAFANA_ALERT_INGEST_ENABLED=false
```

### Semantic Search and External Embeddings

Behavior:

- `SemanticSearchMode.resolve()` returns `keyword` unless semantic search is enabled and the embedding provider is supported.
- Local deterministic `fake` embeddings remain the safest default.
- External embedding sends redacted text, uses timeout/retry/circuit breaker behavior, and returns `None` on failure.
- Keyword search remains available if embedding generation fails.

Rollback:

```bash
SEMANTIC_RUNBOOK_SEARCH_ENABLED=false
EXTERNAL_EMBEDDING_PROVIDER_ENABLED=false
EMBEDDING_PROVIDER=fake
```

## Observability

Relevant metrics use the `agentp_` prefix:

| Metric | Meaning |
|--------|---------|
| `agentp_m9_feature_enabled` | Resolved M9 feature state. |
| `agentp_m9_feature_flag_conflict_total` | Sub-feature or Tempo conflict while global gate is off. |
| `agentp_llm_runbook_draft_total` | LLM draft generation outcomes. |
| `agentp_llm_incident_diff_total` | Incident diff outcomes. |
| `agentp_web_search_requests_total` | Web search attempts by provider/status. |
| `agentp_web_search_blocked_total` | Blocked Web search operations. |
| `agentp_tempo_trace_queries_total` | Tempo trace query outcomes. |
| `agentp_grafana_webhook_ingest_total` | Grafana helper ingest outcomes. |
| `agentp_grafana_webhook_ignored_total` | Ignored Grafana helper payloads. |
| `agentp_semantic_search_queries_total` | Semantic/hybrid search outcomes. |
| `agentp_embedding_jobs_total` | Embedding job outcomes. |
| `agentp_m9_secret_redaction_failures_total` | Secret redaction safety failures. |

## Verification Commands

Focused M9 tests:

```bash
pytest tests/unit/test_m9_feature_flags.py -q
pytest tests/unit/test_m9_ai_extensions.py -q
pytest tests/unit/test_web_search_safety.py -q
pytest tests/unit/test_tempo_endpoint_detection.py -q
pytest tests/unit/test_grafana_alert_parser.py -q
pytest tests/unit/test_semantic_runbook_search.py -q
pytest tests/unit/test_external_embedding_provider.py -q
pytest tests/e2e/test_m9_ai_extensions.py -q
pytest tests/e2e/test_m9_tempo_grafana.py -q
pytest tests/e2e/test_m9_semantic_search.py -q
```

Full backend gate:

```bash
pytest tests/unit tests/integration \
  --cov=apps --cov=packages \
  --cov-report=term-missing --cov-report=xml \
  --cov-fail-under=80
```

## Rollback Playbooks

Single-capability rollback:

```bash
RUNBOOK_LLM_GENERATION_ENABLED=false
LLM_INCIDENT_DIFF_ENABLED=false
RUNBOOK_WEB_SEARCH_ENABLED=false
TEMPO_DISCOVERY_ENABLED=false
GRAFANA_ALERT_INGEST_ENABLED=false
SEMANTIC_RUNBOOK_SEARCH_ENABLED=false
EXTERNAL_EMBEDDING_PROVIDER_ENABLED=false
```

Full M9 rollback:

```bash
M9_EXTENSIONS_ENABLED=false
TRACE_BACKEND="${PRE_M9_TRACE_BACKEND:-fixture}"
TRACE_ENABLED="${PRE_M9_TRACE_ENABLED:-true}"
LLM_EXTERNAL_PROVIDER_ALLOWED=false
RUNBOOK_WEB_SEARCH_PROVIDER=disabled
EMBEDDING_PROVIDER=fake
```

After rollback:

1. Restart API, worker, and beat.
2. Confirm `/readyz` returns ready.
3. Confirm `agentp_m9_feature_flag_conflict_total` is not increasing unexpectedly.
4. Run a deterministic FakeLLM smoke alert.
5. Verify no pending M9 draft/amendment was auto-published.
