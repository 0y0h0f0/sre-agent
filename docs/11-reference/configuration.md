# 配置参考

配置来自 `packages/common/settings.py`，支持 `.env` 和环境变量。

## 基础依赖

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DATABASE_URL` | `postgresql+psycopg://sre:sre@localhost:5432/sre` | 主数据库 |
| `REDIS_URL` | `redis://localhost:6379/0` | Redis cache/pubsub |
| `CELERY_BROKER_URL` | `redis://localhost:6379/1` | Celery broker |
| `CELERY_RESULT_BACKEND` | `redis://localhost:6379/2` | Celery result backend |
| `PROMETHEUS_URL` | `http://localhost:9090` | Prometheus |
| `LOKI_URL` | `http://localhost:3100` | Loki |
| `OTEL_COLLECTOR_URL` | `http://localhost:4318` | OTel collector |
| `TOOL_TIMEOUT_SECONDS` | `2.0` | 工具 timeout |

## Fixture 路径

| 变量 | 默认值 |
| --- | --- |
| `TRACE_FIXTURE_PATH` | `demo/faults/traces.json` |
| `GIT_CHANGES_FIXTURE_PATH` | `demo/faults/git_changes.json` |
| `SERVICE_TOPOLOGY_PATH` | `demo/topology.json` |
| `K8S_FIXTURE_PATH` | `demo/faults/k8s.json` |
| `DB_DIAGNOSTICS_FIXTURE_PATH` | `demo/faults/db_diagnostics.json` |

## Tool layer

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `METRICS_SERVICE_LABEL` | `service` | Prometheus service label |
| `LOGS_SERVICE_LABEL` | `service` | Loki service label |
| `METRICS_STEP_SECONDS` | `30` | Prometheus query step |
| `METRICS_MAX_WINDOW_SECONDS` | `3600` | 最大单窗口 |
| `METRICS_MAX_SHARDS` | `6` | 最大 shard 数 |
| `TRACE_BACKEND` | `fixture` | `fixture`、`jaeger`、`tempo` |
| `JAEGER_URL` | `http://localhost:16686` | Jaeger |
| `TEMPO_URL` | `http://localhost:3200` | Tempo |
| `DEPLOYMENT_BACKEND` | `fixture` | `fixture`、`github`、`argocd` |
| `GITHUB_API_URL` | `https://api.github.com` | GitHub API |
| `GITHUB_REPO` | `None` | repo |
| `GITHUB_TOKEN` | `None` | secret |
| `ARGOCD_URL` | `http://localhost:8080` | Argo CD |
| `ARGOCD_TOKEN` | `None` | secret |
| `K8S_BACKEND` | `fixture` | `fixture`、`live` |
| `K8S_NAMESPACE` | `default` | namespace |
| `DB_DIAGNOSTICS_BACKEND` | `fixture` | `fixture`、`live` |
| `DB_DIAGNOSTICS_URL` | `None` | read-only diagnostics DSN |
| `DB_DIAGNOSTICS_STATEMENT_TIMEOUT_MS` | `2000` | SQL timeout |

## RAG 与 embedding

| 变量 | 默认值 |
| --- | --- |
| `EMBEDDING_PROVIDER` | `fake` |
| `EMBEDDING_BGE_ZH_URL` | `http://localhost:8083` |
| `EMBEDDING_TEXT2VEC_URL` | `http://localhost:8084` |
| `RUNBOOK_HYBRID_SEARCH_ENABLED` | `true` |
| `RUNBOOK_HYBRID_ALPHA_KEYWORD` | `0.65` |
| `RUNBOOK_HYBRID_ALPHA_NL` | `0.35` |
| `RERANKER_PROVIDER` | `fake` |
| `RERANKER_COHERE_MODEL` | `rerank-english-v3.0` |
| `RERANKER_JINA_BASE_URL` | `http://localhost:8081/v1` |
| `RERANKER_JINA_MODEL` | `jina-reranker-v2-base-multilingual` |
| `RERANKER_BGE_BASE_URL` | `http://localhost:8082` |
| `RERANKER_BGE_MODEL` | `BAAI/bge-reranker-v2-m3` |

## LLM

| 变量 | 默认值 |
| --- | --- |
| `LLM_PROVIDER` | `fake` |
| `LLM_MODEL` | `fake-diagnosis-model` |
| `LLM_BASE_URL` | `http://localhost:8001/v1` |
| `LLM_API_KEY` | `None` |
| `LLM_TIMEOUT_SECONDS` | `30.0` |
| `LLM_MAX_TOKENS` | `512` |
| `LLM_TEMPERATURE` | `0.1` |
| `LLM_REASONING_ENABLED` | `false` |
| `LLM_REASONING_EFFORT` | `medium` |
| `LLM_REASONING_NODES` | `diagnose` |
| `TOKEN_BUDGET_TOTAL` | `32000` |
| `TOKEN_BUDGET_PROMPT` | `12000` |
| `TOKEN_CACHE_ENABLED` | `true` |

## 邮件

| 变量 | 默认值 |
| --- | --- |
| `SMTP_HOST` | empty |
| `SMTP_PORT` | `587` |
| `SMTP_TLS_MODE` | `auto` |
| `SMTP_TIMEOUT_SECONDS` | `30.0` |
| `SMTP_USER` | `None` |
| `SMTP_PASSWORD` | `None` |
| `SMTP_FROM` | `sre-agent@example.local` |
| `SRE_EMAIL_LIST` | empty |
| `WEB_BASE_URL` | `http://localhost:5173` |
| `NOTIFICATION_TIMEZONE` | `UTC` |

## Memory 与学习

| 变量 | 默认值 |
| --- | --- |
| `NFA_AUTO_SUPPRESS_THRESHOLD` | `3` |
| `NFA_RESET_DAYS` | `30` |
| `CROSS_INCIDENT_SIMILARITY_THRESHOLD` | `0.7` |
| `CROSS_INCIDENT_MAX_RESULTS` | `5` |

## 审批

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `APPROVAL_AUTO_APPROVE_MINUTES` | `0` | 0 表示关闭 |
| `APPROVAL_AUTO_APPROVE_MAX_RISK` | `L2` | 自动审批上限，代码硬限制不超过 L2 |

## Ops

| 变量 | 默认值 |
| --- | --- |
| `API_KEY_AUTH_ENABLED` | `true` |
| `API_KEY_OPEN_PATHS` | `/healthz,/readyz,/metrics,/docs,/openapi.json` |
| `API_KEY_DEFAULT_EXPIRY_DAYS` | `90` |
| `API_KEY_INITIAL_SEED` | `None` |
| `CELERY_METRICS_PORT` | `9800` |
| `PROMETHEUS_METRICS_ENABLED` | `true` |
| `SHADOW_MODE_ENABLED` | `false` |
| `DB_POOL_SIZE` | `5` |
| `DB_MAX_OVERFLOW` | `10` |
| `DB_POOL_RECYCLE_SECONDS` | `3600` |
| `DB_CONNECT_TIMEOUT_SECONDS` | `5` |
| `REDIS_SOCKET_CONNECT_TIMEOUT` | `1.0` |
| `REDIS_SOCKET_TIMEOUT` | `2.0` |
| `REDIS_RETRY_ON_TIMEOUT` | `true` |
| `CORS_ALLOW_ORIGINS` | `http://localhost:5173` |
| `TASK_ORPHAN_TIMEOUT_SECONDS` | `300` |
| `CELERY_TASK_ALWAYS_EAGER` | `false` |
