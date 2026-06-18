# 运维 Runbook

**最后更新：** 2026-06-18

本文面向本地 demo、预生产和生产演练的日常操作。生产环境真实接入前必须同时阅读 [生产环境检查清单](../production-checklist.md) 和 [最终执行前发布门禁](../final-pre-execution-checklist.md)。

需要沿代码路径理解生产发布、运行时 profile、健康检查、M9 rollout 和回滚验证时，见 [生产发布、运维与回滚技术深挖](../00-overview/production-operations-rollback-deep-dive.md)。

## 快速健康检查

```bash
curl http://localhost:8000/healthz
curl http://localhost:8000/readyz
curl http://localhost:8000/metrics
curl http://localhost:9090/-/healthy
curl http://localhost:3100/ready
curl http://localhost:3000/api/health
```

预期：

| Endpoint | 预期 |
|----------|------|
| `GET /healthz` | `{"status":"ok"}` |
| `GET /readyz` | `status=ready`，`dependencies.postgres/redis/celery_broker=ok` |
| `GET /metrics` | Prometheus text format |
| Prometheus `/-/healthy` | HTTP 200 |
| Loki `/ready` | HTTP 200，body `Ready` |
| Grafana `/api/health` | HTTP 200 |

`/healthz` 只说明 API 进程存活；`/readyz` 才检查 DB、Redis 和 Celery broker 依赖。

## Compose 操作

默认启动 13 个服务：

```bash
docker compose up -d
```

启动 dev profile 中的 Mailpit：

```bash
docker compose --profile dev up -d
```

常用命令：

```bash
docker compose ps
docker compose logs -f api worker beat web
docker compose restart api worker
docker compose down
```

扩缩容：

```bash
docker compose up -d --scale api=3
docker compose up -d --scale worker=3
```

`beat` 必须保持单实例，避免重复调度 daily summary、stale approval、Alertmanager poll 和 discovery rerun。

## 数据库操作

完整 Compose 的 `api` 容器启动时已经执行 `alembic upgrade head`。手动确认：

```bash
docker compose exec api alembic current
docker compose exec api alembic upgrade head
docker compose exec postgres psql -U sre -d sre
```

备份和恢复：

```bash
docker compose exec postgres pg_dump -U sre sre > backup_$(date +%Y%m%d_%H%M%S).sql
docker compose exec -T postgres psql -U sre sre < backup.sql
```

生产环境恢复前必须先在隔离环境验证 dump 可用，不要直接覆盖生产库。

## 告警流水线

本地 webhook 演示：

```bash
curl -X POST http://localhost:8080/faults/high-5xx-after-deploy
curl -X POST http://localhost:8000/api/alerts   -H "Content-Type: application/json"   -d @demo/alerts/high-5xx.json
```

核心状态流：

1. API 创建或去重 incident。
2. API 创建 agent run 并入队 Celery。
3. Worker 运行 LangGraph。
4. L2/L3 动作进入 approval；L4 直接拒绝。
5. 审批后 resume，执行 fixture/live executor。
6. Verify/replan/report/persist memory。

告警来源由 `ALERT_SOURCE` 控制：

| 值 | 行为 |
|----|------|
| `webhook` | 仅 `POST /api/alerts`，默认 |
| `poll` | 仅 Alertmanager poll |
| `both` | webhook + poll |
| `none` | 维护模式，不接收告警 |

## 审批操作

首选 React 控制台 `/approvals`。API 示例：

```bash
curl -X POST http://localhost:8000/api/approvals/<approval_id>/approve   -H "Content-Type: application/json"   -d '{"approver":"operator","comment":"approved"}'
```

L3 必须二次确认：

```bash
curl -X POST http://localhost:8000/api/approvals/<approval_id>/approve   -H "Content-Type: application/json"   -d '{
    "approver":"operator",
    "comment":"approved with second confirmation",
    "risk_ack":true,
    "confirm_action_type":"<action.type>",
    "confirm_target":"<action.target>"
  }'
```

不要使用 email token 批准 L3。批量审批也不会自动补齐 L3 二次确认字段。

## 监控指标

API `/metrics` 暴露 `packages/common/metrics.py` 中注册的指标。常用指标：

| 指标 | 说明 |
|------|------|
| `agentp_diagnosis_total` | 诊断完成次数，label: `status`, `model` |
| `agentp_diagnosis_duration_seconds` | 诊断耗时直方图 |
| `agentp_active_diagnoses` | 当前运行中的诊断数 |
| `agentp_tool_call_total` | 工具调用次数，label: `tool_name`, `status` |
| `agentp_tool_cache_hit_total` / `agentp_tool_cache_miss_total` | 工具缓存命中/未命中 |
| `agentp_approval_total` | 审批决策计数 |
| `agentp_approval_response_time_seconds` | 审批响应时长 |
| `agentp_llm_prompt_tokens_total` / `agentp_llm_completion_tokens_total` | LLM token 统计 |
| `agentp_llm_call_errors_total` | LLM 调用错误 |
| `agentp_email_send_total` | 邮件发送结果 |
| `agentp_m9_feature_enabled` | M9 feature 状态 |
| `agentp_m9_feature_flag_conflict_total` | M9 全局关闭但子功能开启的冲突计数 |

Worker 进程在 `PROMETHEUS_METRICS_ENABLED=true` 且非 eager 模式下会尝试启动独立 metrics HTTP server，端口为 `CELERY_METRICS_PORT`，默认 `9800`。Compose 中当前没有把该端口映射到宿主机；需要采集 worker 端口时应在部署配置中显式暴露或由集群 Service 抓取。

## 常见故障排查

### API not ready

1. `docker compose ps postgres redis api`
2. `docker compose exec postgres pg_isready -U sre -d sre`
3. `docker compose exec redis redis-cli ping`
4. `docker compose exec api alembic current`
5. `docker compose logs api`

如果宿主机手动运行 API，确认 `DATABASE_URL` 用 `localhost:5433`，`REDIS_URL` 用 `localhost:6378`。

### Worker 不消费任务

1. 确认 API 和 worker 指向同一个 `CELERY_BROKER_URL`。
2. `docker compose logs worker`
3. 检查 `agent_runs.status` 是否卡在 `queued` 或 `running`。
4. 检查 orphan timeout：`TASK_ORPHAN_TIMEOUT_SECONDS` 默认 300 秒。
5. 如果涉及 checkpoint，确认真实 PostgreSQL 可达；Postgres checkpointer 失败应 fail closed，不允许悄悄跳过 L2/L3 审批。

### 诊断失败或证据不足

1. 确认 `LLM_PROVIDER=fake` 或生产手动设置 `disabled`。
2. 确认默认 tools：`TRACE_BACKEND=fixture`、`DEPLOYMENT_BACKEND=fixture`、`K8S_BACKEND=fixture`、`DB_DIAGNOSTICS_BACKEND=fixture`。
3. `curl http://localhost:9090/-/healthy`。
4. `curl http://localhost:3100/ready`。
5. 查看 worker 日志中的 node trace/tool call 错误。
6. 检查 `TOOL_TIMEOUT_SECONDS`，Compose 默认 2 秒。

### Runbook 搜索无结果

1. 重新 ingest：

```bash
curl -X POST http://localhost:8000/api/runbooks/ingest   -H "Content-Type: application/json"   -d '{"path":"demo/runbooks","reingest":true}'
```

2. 查询：

```bash
curl "http://localhost:8000/api/runbooks/search?q=High5xxAfterDeploy&service=checkout"
```

3. 如果语义搜索或 embedding provider 不可用，应能降级到关键词/混合检索。不要让 embedding 失败阻断 runbook 入库。

### 审批无法恢复

1. 查看 action 风险等级：L2/L3 才进入审批；L4 被直接拒绝。
2. L3 检查 `risk_ack`、`confirm_action_type`、`confirm_target` 是否匹配 action。
3. 多审批 run 需要所有 sibling approvals 都结束后才 resume。
4. 查看 `fake_resume_enqueue` 只适用于测试；真实运行应看 Celery resume task。

### Grafana / M9 Alert Ingest

当前公开 HTTP 路径是通用 `/api/alerts`，它可以归一化 Grafana-shaped payload，并受通用 API key、rate limit、schema validation 和 fingerprint dedup 保护。`AlertService.ingest_grafana_alert()` 是 gated helper，但当前没有独立注册的 Grafana webhook router。

M9 Grafana helper 默认关闭。启用 helper 前确认：

```bash
export M9_EXTENSIONS_ENABLED=true
export GRAFANA_ALERT_INGEST_ENABLED=true
export GRAFANA_WEBHOOK_SECRET_REF=env:GRAFANA_WEBHOOK_SECRET
```

`GRAFANA_WEBHOOK_SECRET_REF` 和 `GRAFANA_WEBHOOK_MAX_BYTES` 当前是配置字段，未被公开 route 用于 HMAC 或 payload size 校验。若未来暴露独立 Grafana webhook route，必须先验证 HMAC、payload size limit 和 fingerprint dedup。

## 安全回滚

Kubernetes live executor 的受控 smoke 和回滚步骤见 [K8s 后端对接验证](../08-deploy/k8s-backend-verification.md#5-live-executor-smoke)。

基础安全回滚：

```bash
export EXECUTOR_BACKEND=fixture
export LLM_PROVIDER=disabled
export DISCOVERY_ENABLED=false
export ALERT_SOURCE=webhook
```

M9 完全回滚：

```bash
export M9_EXTENSIONS_ENABLED=false
export TRACE_BACKEND=${PRE_M9_TRACE_BACKEND}
export TRACE_ENABLED=${PRE_M9_TRACE_ENABLED}
docker compose restart api worker beat
```

子功能回滚优先只关闭对应 flag，例如 `RUNBOOK_WEB_SEARCH_ENABLED=false`、`SEMANTIC_RUNBOOK_SEARCH_ENABLED=false`。不要用关闭全系统的方式代替精确回滚，除非已经确认存在跨功能安全风险。

## 生产禁止项

- 不从控制台或脚本执行真实数据删除、truncate、flush cache、modify database。
- 不把 `EXECUTOR_BACKEND=live` 作为默认值。
- 不让真实 LLM 进入 CI 稳定门禁。
- 不在 DB、日志、审计、prompt、Agent state 中写入原始 secret。
- 不让 M9 子功能绕过 `M9_EXTENSIONS_ENABLED`。
