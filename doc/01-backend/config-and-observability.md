# 配置与后端可观测性

## 环境变量

```text
DATABASE_URL=postgresql+psycopg://sre:sre@postgres:5432/sre
REDIS_URL=redis://redis:6379/0
CELERY_BROKER_URL=redis://redis:6379/1
CELERY_RESULT_BACKEND=redis://redis:6379/2
PROMETHEUS_URL=http://prometheus:9090
LOKI_URL=http://loki:3100
OTEL_COLLECTOR_URL=http://otel-collector:4318
LLM_PROVIDER=fake
EMBEDDING_PROVIDER=fake
TOKEN_CACHE_ENABLED=true
TOKEN_BUDGET_TOTAL=32000
TOKEN_BUDGET_PROMPT=12000

# Email notifications / real SMTP smoke test
RUN_REAL_EMAIL_TEST=false
SMTP_HOST=
SMTP_PORT=587
SMTP_TLS_MODE=auto
SMTP_TIMEOUT_SECONDS=30
SMTP_USER=
SMTP_PASSWORD=
SMTP_FROM=
SRE_EMAIL_LIST=
WEB_BASE_URL=http://localhost:5173
NOTIFICATION_TIMEZONE=Asia/Shanghai
```

## 日志格式

使用结构化 JSON 日志：

```json
{
  "timestamp": "2026-05-31T10:00:00Z",
  "level": "INFO",
  "request_id": "req_123",
  "incident_id": "inc_123",
  "agent_run_id": "run_123",
  "message": "agent node completed",
  "extra": {
    "node": "collect_logs",
    "duration_ms": 1200
  }
}
```

## Trace span

必须为以下操作加 span：

- HTTP request。
- Celery task。
- LangGraph node。
- Tool call。
- RAG retrieval。
- LLM call。
- Context compression。

## Metrics

暴露 `/metrics`，至少包含：

- `api_request_duration_seconds`
- `celery_task_duration_seconds`
- `agent_run_duration_seconds`
- `agent_node_duration_seconds`
- `tool_call_duration_seconds`
- `tool_call_failures_total`
- `llm_prompt_tokens_total`
- `llm_completion_tokens_total`
- `llm_cache_hits_total`
- `llm_cache_misses_total`
- `context_compressions_total`

## 请求追踪

- 每个请求有 `request_id`。
- 每个 incident 有 `incident_id`。
- 每个 Agent 执行有 `agent_run_id`。
- 每个工具调用有 `tool_call_id`。
- 日志、数据库记录、前端展示都必须保留这些 ID。
