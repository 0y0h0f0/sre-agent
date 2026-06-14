# Configuration Reference

**Last updated:** 2026-06-14

The runtime source of truth is `packages/common/settings.py`. This document explains the stable settings surface for developers and operators.

Settings are loaded by `pydantic-settings` from environment variables and `.env`. Environment variable names are the uppercase form of the field name, for example `database_url` maps to `DATABASE_URL`.

## Precedence and Runtime Sources

There are two related configuration layers:

1. Application settings: `Settings` reads `.env`, process environment, and built-in defaults.
2. Effective backend config: discovery/config publishing can merge backend URLs with this priority:

```text
env > active override > profile > published EffectiveConfigVersion > safe default
```

Important rules:

- Explicit environment variables always win.
- Local/demo mode may use localhost defaults.
- Production effective config does not fall back to localhost backend URLs when using operator-source merging.
- `APP_ENV=production` applies two safety defaults only when those fields are not explicitly set: `LLM_PROVIDER=disabled` and `DISCOVERY_ENABLED=false`.
- `EXECUTOR_BACKEND` is not auto-rewritten by the production validator; production rollout must explicitly confirm it remains `fixture` unless a live K8s executor rollout is separately approved.

## Compose and `.env.example` Differences

The Python defaults are library/runtime defaults. Docker Compose may override them for the local stack. Notable local Compose overrides include:

| Setting | Python default | Compose local default | Meaning |
|---------|----------------|-----------------------|---------|
| `DATABASE_URL` | `postgresql+psycopg://sre:sre@localhost:5432/sre` | container service URL | API/worker connect to the Compose Postgres service. |
| `REDIS_URL` | `redis://localhost:6379/0` | container service URL | API/worker connect to Compose Redis. |
| `TRACE_ENABLED` | `true` | `true` | Trace tool active by default in Compose. |
| `TRACE_BACKEND` | `fixture` | `fixture` | Deterministic trace fixtures by default. |
| `EXECUTOR_BACKEND` | `fixture` | `fixture` | No real external mutation by default. |
| `API_KEY_AUTH_ENABLED` | `true` | often disabled for local Compose | Local demo may skip auth; production must not. |

`.env.example` is a conservative manual baseline and may disable some local integrations for hand-run development. Compose is the source for the one-command local demo.

## Core Services

| Variable | Default | Notes |
|----------|---------|-------|
| `DATABASE_URL` | `postgresql+psycopg://sre:sre@localhost:5432/sre` | SQLAlchemy URL for the primary database. |
| `REDIS_URL` | `redis://localhost:6379/0` | General Redis URL for cache/locks. |
| `CELERY_BROKER_URL` | `redis://localhost:6379/1` | Celery broker. |
| `CELERY_RESULT_BACKEND` | `redis://localhost:6379/2` | Celery result backend. |
| `CORS_ALLOW_ORIGINS` | `http://localhost:5173` | Comma-separated browser origins. |
| `WEB_BASE_URL` | `http://localhost:5173` | Used in notifications/links. |
| `APP_ENV` | `local` | `production` activates limited safety defaults. |

## Observability Backends

| Variable | Default | Notes |
|----------|---------|-------|
| `PROMETHEUS_URL` | `http://localhost:9090` | Metrics tool backend. |
| `LOKI_URL` | `http://localhost:3100` | Logs tool backend. |
| `OTEL_COLLECTOR_URL` | `http://localhost:4318` | OTel collector endpoint. |
| `JAEGER_URL` | `http://localhost:16686` | Jaeger query UI/API base URL. |
| `TEMPO_URL` | `http://localhost:3200` | Native Tempo API base URL. |
| `ALERTMANAGER_URL` | `http://localhost:9093` | Alertmanager read API. |
| `ALERTMANAGER_READ_TOKEN` | unset | Optional read token, stored as `SecretStr`. |
| `BACKEND_URL_ALLOWLIST` | empty | Comma-separated host patterns for production URL safety exceptions. |

## Tool Layer

| Variable | Default | Allowed / effect |
|----------|---------|------------------|
| `TOOL_TIMEOUT_SECONDS` | `2.0` | Default timeout for tool/backend HTTP calls. |
| `METRICS_SERVICE_LABEL` | `service` | Label used in PromQL selectors. |
| `LOGS_SERVICE_LABEL` | `service` | Label used in LogQL selectors. |
| `METRICS_STEP_SECONDS` | `30` | Prometheus query step. |
| `METRICS_MAX_WINDOW_SECONDS` | `3600` | Maximum metrics query window. |
| `METRICS_MAX_SHARDS` | `6` | Max sharding count for large queries. |
| `TRACE_ENABLED` | `true` | `false` forces degraded trace backend. |
| `TRACE_BACKEND` | `fixture` | `disabled`, `fixture`, `jaeger`, `tempo`. |
| `TRACE_FIXTURE_PATH` | `demo/faults/traces.json` | Fixture trace data path. |
| `DEPLOYMENT_BACKEND` | `fixture` | `fixture`, `github`, `argocd`. |
| `GIT_CHANGES_FIXTURE_PATH` | `demo/faults/git_changes.json` | Fixture deployment-change data. |
| `GITHUB_API_URL` | `https://api.github.com` | GitHub adapter base URL. |
| `GITHUB_REPO` | unset | Repository slug for GitHub adapter. |
| `GITHUB_TOKEN` | unset | Optional secret token. |
| `ARGOCD_URL` | `http://localhost:8080` | Argo CD adapter base URL. |
| `ARGOCD_TOKEN` | unset | Optional secret token. |
| `K8S_BACKEND` | `fixture` | `fixture` or read-only `live`. |
| `K8S_FIXTURE_PATH` | `demo/faults/k8s.json` | K8s fixture data. |
| `K8S_NAMESPACE` | `default` | Namespace for K8s read diagnostics. |
| `DB_DIAGNOSTICS_BACKEND` | `fixture` | `fixture` or read-only `live`. |
| `DB_DIAGNOSTICS_FIXTURE_PATH` | `demo/faults/db_diagnostics.json` | DB diagnostic fixture data. |
| `DB_DIAGNOSTICS_URL` | unset | Live read-only diagnostic database URL. |
| `DB_DIAGNOSTICS_STATEMENT_TIMEOUT_MS` | `2000` | Statement timeout for live read-only DB diagnostics. |
| `SERVICE_TOPOLOGY_PATH` | `demo/topology.json` | Optional topology fixture for cascading-failure analysis. |

## Executor

| Variable | Default | Notes |
|----------|---------|-------|
| `EXECUTOR_BACKEND` | `fixture` | `fixture` is default for tests, CI, and local demo. `live` is explicit operator opt-in. |
| `EXECUTOR_TIMEOUT_SECONDS` | `30.0` | Timeout for executor operations. |
| `EXECUTOR_K8S_NAMESPACE` | `default` | Namespace for live K8s executor operations. |

`EXECUTOR_BACKEND=live` may perform only the existing guarded Kubernetes mutations after guardrails and approval:

- rolling restart for `restart_pod` / `restart_service`,
- deployment scale patch for `scale_deployment` / `scale_back`,
- deployment rollback subresource call for `rollback_release` (`rollback_deployment` is normalized to this same operation).

It must not perform cloud writes, database mutations, data deletion, cache flushes, or arbitrary Kubernetes writes.

## RAG and Embeddings

| Variable | Default | Notes |
|----------|---------|-------|
| `EMBEDDING_PROVIDER` | `fake` | `fake`, `bge_zh`, `text2vec` in the base factory. |
| `EMBEDDING_BGE_ZH_URL` | `http://localhost:8083` | Local BGE-ZH embedding endpoint. |
| `EMBEDDING_TEXT2VEC_URL` | `http://localhost:8084` | Local text2vec endpoint. |
| `RUNBOOK_HYBRID_SEARCH_ENABLED` | `true` | Enables keyword/vector weighted retrieval. |
| `RUNBOOK_HYBRID_ALPHA_KEYWORD` | `0.65` | Keyword weight. |
| `RUNBOOK_HYBRID_ALPHA_NL` | `0.35` | Vector/NL weight. |
| `RERANKER_PROVIDER` | `fake` | Reranker backend selector. |
| `RERANKER_COHERE_API_KEY` | unset | Optional secret token. |
| `RERANKER_COHERE_MODEL` | `rerank-english-v3.0` | Cohere model name. |
| `RERANKER_JINA_BASE_URL` | `http://localhost:8081/v1` | Jina-compatible local/HTTP endpoint. |
| `RERANKER_JINA_MODEL` | `jina-reranker-v2-base-multilingual` | Jina model. |
| `RERANKER_BGE_BASE_URL` | `http://localhost:8082` | BGE reranker endpoint. |
| `RERANKER_BGE_MODEL` | `BAAI/bge-reranker-v2-m3` | BGE reranker model. |
| `RUNBOOK_TEMPLATE_GENERATION_ENABLED` | `true` | Deterministic template generation. |
| `RUNBOOK_LLM_GENERATION_ENABLED` | `false` | M9 LLM draft generation gate. |
| `RUNBOOK_WEB_SEARCH_ENABLED` | `false` | M9 Web context gate. |
| `RUNBOOK_AMENDMENT_MIN_INCIDENTS` | `5` | Minimum recurring incidents before deterministic amendment feedback. |
| `RUNBOOK_AMENDMENT_COOLDOWN_DAYS` | `7` | Cooldown for repeated amendment drafts. |

Current database vector dimensions:

- `runbook_chunks.embedding`: current schema uses 512-dimensional vectors for FakeEmbedding/BGE-ZH paths.
- `memory_items.embedding`: nullable 512-dimensional vector field in the current schema.
- `text2vec` provider returns 1024-dimensional vectors and should be used only where schema/index compatibility is handled.

## LLM

| Variable | Default | Notes |
|----------|---------|-------|
| `LLM_PROVIDER` | `fake` | `fake`, `vllm`, `openai`, `deepseek`, `anthropic`, or `disabled` in production safety paths. |
| `LLM_MODEL` | `fake-diagnosis-model` | Model name passed to provider adapters. |
| `LLM_BASE_URL` | `http://localhost:8001/v1` | Local/OpenAI-compatible base URL. |
| `LLM_API_KEY` | unset | Optional `SecretStr`. |
| `LLM_TIMEOUT_SECONDS` | `30.0` | Provider call timeout. |
| `LLM_MAX_TOKENS` | `512` | Max completion tokens. |
| `LLM_TEMPERATURE` | `0.1` | Generation temperature. |
| `LLM_REASONING_ENABLED` | `false` | Enables deep reasoning output where supported. |
| `LLM_REASONING_EFFORT` | `medium` | Reasoning effort hint. |
| `LLM_REASONING_NODES` | `diagnose,diagnose_synthesize` | Comma-separated nodes using reasoning mode. |
| `LLM_MULTI_PERSPECTIVE_ENABLED` | `false` | Optional multi-perspective diagnosis mode. |
| `TOKEN_BUDGET_TOTAL` | `32000` | Overall token budget. |
| `TOKEN_BUDGET_PROMPT` | `12000` | Prompt budget. |
| `TOKEN_CACHE_ENABLED` | `true` | Application prompt segment cache toggle. |

CI and deterministic tests must use FakeLLM. Real LLMs are for manual demos/evals only, not stable CI gates.

## M9 Controlled Enhancements

| Variable | Default | Notes |
|----------|---------|-------|
| `M9_EXTENSIONS_ENABLED` | `false` | Global M9 gate. Must be true for sub-features to resolve enabled. |
| `RUNBOOK_WEB_SEARCH_PROVIDER` | `disabled` | `disabled` or `fake` in current implementation; unknown providers degrade to disabled. |
| `RUNBOOK_WEB_SEARCH_TIMEOUT_SECONDS` | `10` | Web search timeout. |
| `RUNBOOK_WEB_SEARCH_MAX_RESULTS` | `5` | Max search results, capped by schema. |
| `RUNBOOK_WEB_SEARCH_REQUIRE_HTTPS` | `true` | HTTPS policy for external Web results. |
| `RUNBOOK_WEB_SEARCH_ALLOWED_DOMAINS` | empty | Required in production for Web search. |
| `RUNBOOK_WEB_SEARCH_BLOCKED_DOMAINS` | empty | Blocklist; should override allowlist policy. |
| `RUNBOOK_WEB_SEARCH_MAX_CONTENT_BYTES` | `1048576` | Per-result content cap. |
| `RUNBOOK_WEB_SEARCH_CACHE_TTL_SECONDS` | `86400` | Web context cache TTL. |
| `RUNBOOK_WEB_SEARCH_MAX_REDIRECTS` | `3` | Redirect cap. |
| `GRAFANA_WEBHOOK_SECRET_REF` | empty | Secret reference for Grafana webhook integrations. |
| `GRAFANA_WEBHOOK_MAX_BYTES` | `256000` | Grafana webhook payload size policy. |
| `LLM_INCIDENT_DIFF_ENABLED` | `false` | M9 incident/runbook diff gate. |
| `MIN_INCIDENT_DIFF_EVIDENCE_REFS` | `5` | Evidence ref threshold when no report/feedback/action/version evidence is present. |
| `TEMPO_DISCOVERY_ENABLED` | `false` | M9 Tempo endpoint discovery gate. |
| `GRAFANA_ALERT_INGEST_ENABLED` | `false` | M9 Grafana webhook helper gate. |
| `SEMANTIC_RUNBOOK_SEARCH_ENABLED` | `false` | M9 semantic/hybrid runbook search gate. |
| `EXTERNAL_EMBEDDING_PROVIDER_ENABLED` | `false` | M9 external embedding gate. |
| `LLM_EXTERNAL_PROVIDER_ALLOWED` | `false` | Required for cloud LLM providers in M9 draft/diff flows. |
| `PRE_M9_TRACE_BACKEND` | empty | Operator-recorded rollback value. |
| `PRE_M9_TRACE_ENABLED` | empty | Operator-recorded rollback value. |

M9 rollback defaults:

```bash
M9_EXTENSIONS_ENABLED=false
RUNBOOK_LLM_GENERATION_ENABLED=false
LLM_INCIDENT_DIFF_ENABLED=false
RUNBOOK_WEB_SEARCH_ENABLED=false
TEMPO_DISCOVERY_ENABLED=false
GRAFANA_ALERT_INGEST_ENABLED=false
SEMANTIC_RUNBOOK_SEARCH_ENABLED=false
EXTERNAL_EMBEDDING_PROVIDER_ENABLED=false
LLM_EXTERNAL_PROVIDER_ALLOWED=false
RUNBOOK_WEB_SEARCH_PROVIDER=disabled
EMBEDDING_PROVIDER=fake
TRACE_BACKEND="${PRE_M9_TRACE_BACKEND:-fixture}"
TRACE_ENABLED="${PRE_M9_TRACE_ENABLED:-true}"
```

## Discovery and Automation

| Variable | Default | Notes |
|----------|---------|-------|
| `AUTOMATION_LEVEL` | `supervised` | Discovery/proposal automation profile. |
| `DISCOVERY_ENABLED` | `true` locally, `false` in production when unset | Backend discovery master switch. |
| `DISCOVERY_MANUAL_RERUN_ENABLED` | `true` | Allows operator-triggered rerun APIs/tasks. |
| `DISCOVERY_APPLY_MODE` | `inherit` | `inherit`, `propose`, or `supervised`. |

Published effective config statuses are documented in [Status and IDs](status-and-ids.md). Workers use only the latest `published` effective config and otherwise rely on settings/defaults.

## Alert Ingestion and Polling

| Variable | Default | Notes |
|----------|---------|-------|
| `ALERT_SOURCE` | `webhook` | `webhook`, `poll`, `both`, or `none`. |
| `ALERT_POLL_INTERVAL_SECONDS` | `30` | Beat poll interval. |
| `ALERT_POLL_LOCK_TTL_SECONDS` | `60` | Redis lock TTL. |
| `ALERT_POLL_TIMEOUT_SECONDS` | `20` | Poll request timeout. |
| `ALERT_POLL_RESOLVED_GRACE_PERIOD_SECONDS` | `120` | Missing duration before resolved inference. |
| `ALERT_POLL_RESOLVED_MISSING_ROUNDS` | `3` | Consecutive missing rounds before resolved inference. |
| `ALERT_POLL_RECEIVER_FILTER` | empty | Pipe-separated receiver names. |
| `ALERT_POLL_FILTER_MATCHERS` | empty | Alertmanager matcher expressions. |
| `ALERT_POLL_NAMESPACE_ALLOWLIST` | empty | Namespace allowlist. |
| `ALERT_POLL_SERVICE_ALLOWLIST` | empty | Service allowlist. |
| `ALERT_POLL_MAX_ALERTS_PER_ROUND` | `200` | Per-round alert cap. |
| `ALERT_POLL_MAX_NEW_INCIDENTS_PER_ROUND` | `20` | New incident cap per round. |
| `ALERT_POLL_MAX_INCIDENTS_PER_SERVICE_PER_MINUTE` | `5` | Per-service creation cap. |

## Email, Notifications, and Approval

| Variable | Default | Notes |
|----------|---------|-------|
| `SMTP_HOST` | empty | Empty disables real SMTP sending. |
| `SMTP_PORT` | `587` | SMTP port. |
| `SMTP_TLS_MODE` | `auto` | TLS mode policy. |
| `SMTP_TIMEOUT_SECONDS` | `30.0` | SMTP timeout. |
| `SMTP_USER` | unset | Optional username. |
| `SMTP_PASSWORD` | unset | Optional secret password. |
| `SMTP_FROM` | `sre-agent@example.local` | Sender address. |
| `SRE_EMAIL_LIST` | empty | Comma-separated recipients. |
| `NOTIFICATION_TIMEZONE` | `UTC` | Display timezone for notifications. |
| `APPROVAL_AUTO_APPROVE_MINUTES` | `0` | Stale auto-approve disabled by default. |
| `APPROVAL_AUTO_APPROVE_MAX_RISK` | `L2` | Max stale auto-approve risk if enabled. |

L2 and L3 actions still require approval. L3 approval requires `risk_ack=true`, `confirm_action_type`, and `confirm_target`.

## Auth, Rate Limit, and API Keys

| Variable | Default | Notes |
|----------|---------|-------|
| `API_KEY_AUTH_ENABLED` | `true` | Enables bearer API key middleware. Local Compose may override this. |
| `API_KEY_OPEN_PATHS` | `/healthz,/readyz,/metrics,/docs,/openapi.json,/api/approvals/by-token` | Boundary-aware open path list. |
| `API_KEY_DEFAULT_EXPIRY_DAYS` | `90` | Default expiry for generated keys where used. |
| `API_KEY_INITIAL_SEED` | unset | Bootstrap seed accepted by middleware as `apik_initial`. Rotate after use. |
| `RATE_LIMIT_MAX_REQUESTS` | `10` | Request cap per window for alert ingestion. |
| `RATE_LIMIT_WINDOW_SECONDS` | `60` | Rate limit window. |

API key creation currently returns raw key once and stores only a SHA-256 hash. Scope enforcement is skipped when `API_KEY_AUTH_ENABLED=false`.

## Learning, Correlation, and Evals

| Variable | Default | Notes |
|----------|---------|-------|
| `NFA_AUTO_SUPPRESS_THRESHOLD` | `3` | Number of NFA marks before pattern suppression. |
| `NFA_RESET_DAYS` | `30` | False-positive pattern reset horizon. |
| `CROSS_INCIDENT_SIMILARITY_THRESHOLD` | `0.7` | Similar incident threshold. |
| `CROSS_INCIDENT_MAX_RESULTS` | `5` | Max cross-incident matches. |
| `SHADOW_MODE_ENABLED` | `false` | Shadow eval mode toggle. |
| `CELERY_TASK_ALWAYS_EAGER` | `false` | Test/dev option for eager Celery execution. |

## Metrics and HA

| Variable | Default | Notes |
|----------|---------|-------|
| `CELERY_METRICS_PORT` | `9800` | Worker metrics port. |
| `PROMETHEUS_METRICS_ENABLED` | `true` | Metrics collection/export toggle. |
| `DB_POOL_SIZE` | `5` | SQLAlchemy pool size. |
| `DB_MAX_OVERFLOW` | `10` | SQLAlchemy pool overflow. |
| `DB_POOL_RECYCLE_SECONDS` | `3600` | Pool recycle interval. |
| `DB_CONNECT_TIMEOUT_SECONDS` | `5` | DB connect timeout. |
| `REDIS_SOCKET_CONNECT_TIMEOUT` | `1.0` | Redis connect timeout. |
| `REDIS_SOCKET_TIMEOUT` | `2.0` | Redis socket timeout. |
| `REDIS_RETRY_ON_TIMEOUT` | `true` | Redis retry policy. |
| `TASK_ORPHAN_TIMEOUT_SECONDS` | `300` | Running task orphan timeout before re-execution logic can treat it as stuck. |

## Production Minimums

Before production rollout, confirm these explicitly:

```bash
APP_ENV=production
API_KEY_AUTH_ENABLED=true
EXECUTOR_BACKEND=fixture
LLM_PROVIDER=disabled   # unless a manual non-CI provider rollout is approved
M9_EXTENSIONS_ENABLED=false
RUNBOOK_WEB_SEARCH_PROVIDER=disabled
DISCOVERY_ENABLED=false # unless discovery rollout is approved
```

If enabling live diagnostics, keep them read-only:

```bash
K8S_BACKEND=live
DB_DIAGNOSTICS_BACKEND=live
DB_DIAGNOSTICS_URL=<read-only database URL>
```

If enabling the live executor, treat it as a separate high-risk rollout and keep it limited to the supported K8s restart/scale/rollback mutations.
