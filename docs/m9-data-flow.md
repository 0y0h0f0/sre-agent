# M9 Data Flow

**Last updated:** 2026-06-14

This document describes the runtime data movement introduced by M9. It focuses on where data enters, how it is redacted or gated, what is persisted, and how failures degrade. It complements [M9 Rollout](m9-rollout.md) and [M9 Threat Model](m9-threat-model.md).

## Shared Gate Resolution

```text
Environment / Settings
        |
        v
packages.common.settings.Settings
        |
        v
packages.common.feature_flags.resolve_m9_feature_flags()
        |
        +--> global gate: M9_EXTENSIONS_ENABLED
        +--> sub-feature gates
        +--> conflict logs + agentp_m9_feature_flag_conflict_total
```

Most M9 domain code calls `is_m9_subfeature_enabled(settings, "<feature>")` before doing external or AI work. A sub-feature is effective only when the global M9 gate and the individual flag are both true.

Important implementation nuance:

- Native trace backend construction still follows `TRACE_ENABLED` and `TRACE_BACKEND`; the feature flag resolver reports `TRACE_BACKEND=tempo` as a conflict when M9 is disabled.
- The generic `/api/alerts` endpoint can normalize Grafana-shaped payloads. The separate Grafana webhook helper is the gated M9 path.

## Flow 1: LLM Runbook Draft Generation

```text
POST /api/runbooks/llm-generate
        |
        v
require scope: runbook:review OR runbook:llm_generate
        |
        v
RunbookService.llm_generate_draft()
        |
        v
LLMRunbookGenerator.generate()
        |
        +--> M9 + RUNBOOK_LLM_GENERATION gate
        +--> external provider gate: LLM_EXTERNAL_PROVIDER_ALLOWED
        +--> RunbookPromptBuilder.build()
        |       +--> redact service / incident type / evidence / config
        |       +--> include approved context, deterministic template, capability gaps
        |
        +--> LLMProvider.invoke()
        |
        +--> RunbookActionClassifier.classify()
        |
        v
RunbookDraft(drf_, status=pending_review, draft_type=llm_generated)
```

Inputs:

- `service`
- `incident_type`
- optional approved runbook context
- optional evidence summary and evidence IDs
- optional deterministic template draft
- optional capability gaps
- optional effective config snapshot

Redaction:

- Prompt builder applies `redact_text()` and `redact_dict_values()`.
- Redacted metadata includes prompt template version, redaction version, input hash, evidence IDs, prompt preview, and generated output hash.

Persistence:

- Only the draft and metadata are persisted.
- Draft starts as `pending_review`.
- Publishing requires `POST /api/runbooks/drafts/{draft_id}/review` with `status=published`.

Failure modes:

| Failure | Result |
|---------|--------|
| M9/global/sub-feature disabled | `status=disabled`, no draft |
| External LLM without explicit allow | `status=blocked`, no draft |
| LLM invocation exception | `status=degraded`, no draft |
| Insufficient LLM output | `status=degraded`, no draft |

## Flow 2: LLM Incident Diff

```text
POST /api/runbooks/incident-diff
        |
        v
require scope: runbook:review OR incident:llm_diff
        |
        v
RunbookService.llm_incident_diff()
        |
        v
IncidentDiffAnalyzer.analyze()
        |
        +--> M9 + LLM_INCIDENT_DIFF gate
        +--> external provider gate
        +--> minimum evidence threshold
        +--> redact incident context
        +--> LLMProvider.invoke()
        +--> parse JSON amendment proposals
        |
        v
AmendmentDraft(amd_, status=pending_review)
```

Minimum evidence threshold:

- diagnosis report longer than 20 characters, or
- operator feedback longer than 10 characters, or
- at least one action execution result, or
- linked approved runbook version, or
- at least five evidence refs.

Persistence:

- Each generated proposal becomes one `AmendmentDraft`.
- Review uses `POST /api/runbooks/amendments/{amendment_id}/review`.
- Approved runbook versions are not modified by the diff call.

Failure modes:

| Failure | Result |
|---------|--------|
| Feature disabled | `disabled` |
| External provider blocked | `blocked` |
| Evidence threshold not met | `skipped_insufficient_evidence` |
| LLM invocation or parse degradation | `degraded` or synthesized low-confidence reviewer note |

## Flow 3: Web Search for Runbook Enrichment

```text
POST /api/runbooks/web-search
        |
        v
require scope: runbook:review OR runbook:web_search
        |
        v
RunbookWebContextBuilder.build_context()
        |
        +--> M9 + RUNBOOK_WEB_SEARCH gate
        +--> provider check
        +--> production allowed-domain requirement
        +--> redact query
        +--> WebSearchProvider.search()
        +--> validate final URLs
        |
        v
WebSearchResponse(status, redacted query, traceable results)
```

Inputs:

- `query`
- `purpose`, default `draft_enrichment`

Returned metadata:

- title
- original URL
- final URL
- snippet
- content hash
- provider

Persistence:

- The current endpoint returns search context. It does not auto-ingest, auto-publish, or attach results to a runbook without a reviewer-driven path.

Failure modes:

| Failure | Result |
|---------|--------|
| Feature disabled | `disabled` |
| Provider disabled | `degraded` |
| Production allowed domains missing | `blocked` |
| Provider exception | `degraded` |
| Unsafe result URL | result skipped |

## Flow 4: Native Tempo Trace Backend

```text
Settings
  TRACE_ENABLED=true
  TRACE_BACKEND=tempo
  TEMPO_URL=...
        |
        v
packages.tools.trace_backends.build_trace_backend()
        |
        v
TempoTraceBackend
        |
        +--> /api/traces/{trace_id}
        +--> /api/search
        +--> /api/search?q=<TraceQL>
        |
        v
TraceTool normalized spans
        |
        v
Agent evidence summaries
```

Normalized span shape:

- `trace_id`
- `span_id`
- `name`
- `service`
- `downstream_service`
- `duration_ms`
- `status`
- `start`

Degradation:

- Capability flags can disable trace-by-ID, service search, service filter, or TraceQL.
- Exceptions return empty span lists.
- TraceTool reports degraded evidence upstream instead of failing the whole diagnosis.

## Flow 5: Tempo Endpoint Discovery

```text
K8s service list
        |
        v
BackendEndpointDetector.detect()
        |
        +--> skip Tempo unless M9 + TEMPO_DISCOVERY enabled
        +--> match tempo service name/port patterns
        +--> build service DNS URL
        +--> BackendUrlSafetyValidator.validate()
        |
        v
BackendEndpoints(backend_type=tempo, status=ready/requires_review/rejected/degraded)
```

Production behavior:

- Detected Tempo endpoints become `requires_review`.
- Unsafe Tempo URLs become `rejected`.
- Missing Tempo becomes `unavailable` in production and `degraded` locally.

No auto-publish:

- Discovery results do not become worker runtime config until an effective config version is published through the config API.

## Flow 6: Grafana Alert Input

Two input shapes exist.

### Generic Alert Endpoint

```text
POST /api/alerts
        |
        v
AlertCreateRequest.normalize_provider_payload()
        |
        +--> infer or accept source=grafana
        +--> _from_grafana()
        +--> stable fingerprint excluding volatile Grafana UI fields
        |
        v
AlertService.create_alert()
        |
        +--> incident dedup by open fingerprint
        +--> agent run creation
        +--> Celery enqueue
```

This path is part of the general alert normalization layer. It does not by itself represent the M9 webhook helper.

### Grafana Webhook Helper

```text
raw Grafana unified alerting payload
        |
        v
AlertService.ingest_grafana_alert()
        |
        +--> GRAFANA_ALERT_INGEST_ENABLED gate
        +--> payload shape validation
        +--> grafana_to_alert()
        +--> AlertService.create_alert()
        |
        v
AlertCreateResponse or disabled/malformed result
```

Metrics:

- `agentp_grafana_webhook_ingest_total`
- `agentp_grafana_webhook_ignored_total`

## Flow 7: Semantic Search and Embedding Jobs

```text
Runbook ingest / approved draft ingest
        |
        v
split_markdown_document()
        |
        v
build_embedding_provider(settings)
        |
        +--> fake: deterministic 512-dim local vector
        +--> bge_zh: local HTTP 512-dim provider
        +--> text2vec: local HTTP 1024-dim provider
        |
        v
runbook_chunks.embedding / embedding_model
```

M9 side table:

```text
EmbeddingJob(runbook_chunk_id, provider, model, dimension, text_hash)
        |
        v
dedup key = sha256(chunk:provider:model:dimension:text_hash)[:16]
        |
        v
runbook_chunk_embeddings(status=available/degraded/failed)
```

Search mode resolution:

```text
embedding_provider == disabled
        -> keyword

semantic enabled + provider in fake/bge_zh/external
        -> hybrid

otherwise
        -> keyword
```

Degradation:

- Embedding failures do not block runbook chunk storage.
- Keyword search remains available.
- External embedding returns `None` on failure and can open a circuit breaker after repeated failures.

## Flow 8: External Embedding Provider

```text
ExternalEmbeddingProvider.embed(text)
        |
        +--> circuit breaker check
        +--> redact_text(text)
        +--> resolve secret_ref env:VAR_NAME
        +--> POST endpoint with timeout/retry
        +--> validate embedding response
        |
        v
list[float] or None
```

Safety properties:

- Input text is redacted before leaving the process.
- Auth is resolved from a secret reference at call time.
- Raw secret is never stored in provider repr, DB, logs, or metrics labels.
- Unexpected response shape degrades to `None`.

## Data Classification

| Data | May leave process? | Where stored? | Notes |
|------|--------------------|---------------|-------|
| Raw alert payload | No external M9 call required | `incidents.raw_payload` / normalized fields | Provider normalizers preserve useful labels and annotations. |
| Evidence IDs | Yes, as IDs only in prompts | Prompt metadata / draft metadata | Preserve traceability without raw logs. |
| Raw logs | Should not be sent uncompressed | Evidence/tool records, compressed summaries | Large raw logs stay out of Agent state and prompts. |
| Runbook approved context | May enter LLM prompt after redaction | Prompt preview metadata and draft provenance | Source chunks should remain traceable. |
| Effective config | Only redacted snapshot may enter prompt | Effective config versions and prompt metadata | Secrets/auth fields must not be included. |
| Web query | Redacted query may leave process | Web response only | Production requires allowed domains. |
| External embedding text | Redacted text may leave process | External provider receives redacted text | Failure degrades to keyword. |
| Raw secrets | Never | Not persisted | Use secret refs/env vars. |

## Developer Checklist

When adding or changing an M9 data path:

1. Add or reuse an explicit feature flag.
2. Decide whether the path needs an additional production allowlist.
3. Redact text and nested config before any prompt or external request.
4. Add timeout and degraded result behavior.
5. Preserve IDs and content hashes for traceability.
6. Add metrics with low-cardinality labels only.
7. Add unit tests for disabled, blocked, degraded, and success paths.
8. Add a secret leakage test if data leaves the process.
9. Document rollback in [M9 Rollout](m9-rollout.md).
