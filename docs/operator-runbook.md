# Day-2 运维操作手册

**最后更新：** 2026-06-14

本文是生产化和长期运维入口，偏向 Day-2 操作：服务管理、监控、故障排查、备份恢复、扩缩容、M9 受控增强和回滚。日常本地命令见 [运维 Runbook](10-operations/runbook.md)，生产前门禁见 [生产环境检查清单](production-checklist.md)。

## 服务拓扑

默认 `docker compose up -d` 启动 13 个服务；`mailpit` 仅在 `dev` profile 启动。

| 服务 | 端口 | Profile | 扩缩容 | 说明 |
|------|------|---------|--------|------|
| postgres | `5433 -> 5432` | 默认 | 垂直扩容/托管数据库 | PostgreSQL + pgvector |
| redis | `6378 -> 6379` | 默认 | 垂直扩容/托管 Redis | Celery broker/result、cache、lock |
| prometheus | `9090` | 默认 | 单实例或托管 | API/demo metrics |
| loki | `3100` | 默认 | 单实例或托管 | 日志聚合 |
| promtail | - | 默认 | 每节点/每宿主 | 日志采集 |
| otel-collector | `4317`, `4318` | 默认 | 每节点或集中式 | OTLP 收集 |
| bge-zh | `8083` | 默认 | 单实例/按需 | 本地中文 embedding，默认 fake 不依赖 |
| grafana | `3000` | 默认 | 单实例或托管 | Dashboard |
| demo-service | `8080` | 默认 | 演示环境 | fault injection |
| web | `5173` | 默认 | 1+ | React console |
| api | `8000` | 默认 | 水平扩容 | FastAPI |
| worker | - | 默认 | 水平扩容 | Celery worker |
| beat | - | 默认 | 单实例 | 定时任务 |
| mailpit | `8025`, `1025` | `dev` | 不用于生产 | 本地邮件测试 |

生产部署可以替换 Compose 基础设施为托管服务或 Kubernetes，但安全边界不变：API 入队、worker 诊断、L2/L3 审批、fixture executor 默认、M9 默认关闭。

## 健康检查

| 组件 | 检查 | 通过标准 |
|------|------|----------|
| API liveness | `GET /healthz` | `{"status":"ok"}` |
| API readiness | `GET /readyz` | `status=ready`，依赖 `postgres/redis/celery_broker=ok` |
| API metrics | `GET /metrics` | Prometheus text format |
| Prometheus | `GET /-/healthy` | HTTP 200 |
| Loki | `GET /ready` | HTTP 200 |
| Grafana | `GET /api/health` | HTTP 200 |
| Worker | logs + queue depth | 无持续 retry/error，queue 无长期积压 |

`/healthz` 不能代替 `/readyz`。上线、回滚和扩缩容后都要检查 `/readyz`。

## 常用操作

```bash
docker compose up -d
docker compose --profile dev up -d
docker compose ps
docker compose logs -f api worker beat
docker compose restart api worker
docker compose down
```

扩容：

```bash
docker compose up -d --scale api=3
docker compose up -d --scale worker=3
```

`beat` 保持单实例。多 Beat 会重复触发 summary、stale approval、Alertmanager poll 和 discovery rerun。

## 数据库

```bash
docker compose exec api alembic current
docker compose exec api alembic upgrade head
docker compose exec postgres psql -U sre -d sre
```

备份：

```bash
docker compose exec postgres pg_dump -U sre sre > backup_$(date +%Y%m%d_%H%M%S).sql
```

恢复前必须先在隔离环境验证备份。生产恢复需要明确停写窗口、回滚 owner 和验证步骤。

## 关键指标

API `/metrics` 暴露 `agentp_*` 指标。重点观察：

| 指标 | 用途 |
|------|------|
| `agentp_diagnosis_total` | 诊断完成/失败趋势 |
| `agentp_diagnosis_duration_seconds` | 诊断耗时 |
| `agentp_active_diagnoses` | in-flight run 是否堆积 |
| `agentp_tool_call_total` | 工具调用状态 |
| `agentp_tool_cache_hit_total` / `agentp_tool_cache_miss_total` | 工具缓存效果 |
| `agentp_approval_total` | 审批决策计数 |
| `agentp_approval_response_time_seconds` | 审批时长 |
| `agentp_llm_call_errors_total` | LLM provider 错误 |
| `agentp_email_send_total` | 邮件发送状态 |
| `agentp_m9_feature_enabled` | M9 功能状态 |
| `agentp_m9_feature_flag_conflict_total` | M9 全局门禁冲突 |

Worker metrics server 默认端口 `9800`，由 `PROMETHEUS_METRICS_ENABLED` 和 `CELERY_METRICS_PORT` 控制。Compose 未映射该端口；生产采集需要在部署层显式暴露。

## 告警来源

`ALERT_SOURCE`：

| 值 | 行为 |
|----|------|
| `webhook` | 仅 API webhook，默认 |
| `poll` | 仅 Alertmanager poll |
| `both` | webhook + poll |
| `none` | 维护模式 |

启用 Alertmanager poll 前必须验证 scope：receiver、matcher、namespace allowlist、service allowlist、每轮最大 alert、新 incident 限速和 Redis lock。

## 故障排查

### API not ready

1. `docker compose ps postgres redis api`
2. `docker compose exec postgres pg_isready -U sre -d sre`
3. `docker compose exec redis redis-cli ping`
4. `docker compose exec api alembic current`
5. `docker compose logs api`

### Worker 卡住

1. 检查 `CELERY_BROKER_URL` 是否与 API 一致。
2. 检查 Redis、worker logs、agent_runs 状态。
3. 检查 checkpoint：真实 Postgres 不可达时应 fail closed。
4. 检查 `TASK_ORPHAN_TIMEOUT_SECONDS`，默认 300 秒。

### 审批无法推进

1. 确认 action risk：L2/L3 需要审批，L4 直接拒绝。
2. L3 需要 `risk_ack=true`、`confirm_action_type`、`confirm_target`。
3. 多审批 run 只有全部 sibling approvals 决定后才 resume。
4. L3 不允许 email token 审批。

### Runbook 搜索无结果

1. 确认已 ingest runbooks。
2. 检查 `runbook_chunks` 和 source path。
3. 检查 embedding provider 是否 degraded。
4. 使用关键词查询验证降级路径。

## M9 操作

M9 默认关闭。启用任一子功能前必须先记录 pre-M9 trace 状态：

```bash
export PRE_M9_TRACE_BACKEND=<current TRACE_BACKEND>
export PRE_M9_TRACE_ENABLED=<current TRACE_ENABLED>
```

全局和子功能开关：

```bash
export M9_EXTENSIONS_ENABLED=true
export RUNBOOK_LLM_GENERATION_ENABLED=true
export LLM_INCIDENT_DIFF_ENABLED=true
export RUNBOOK_WEB_SEARCH_ENABLED=true
export TEMPO_DISCOVERY_ENABLED=true
export GRAFANA_ALERT_INGEST_ENABLED=true
export SEMANTIC_RUNBOOK_SEARCH_ENABLED=true
export EXTERNAL_EMBEDDING_PROVIDER_ENABLED=true
```

外部 provider 还需要独立双重确认：

```bash
export LLM_EXTERNAL_PROVIDER_ALLOWED=true
export EMBEDDING_PROVIDER=external
```

生产要求：

- LLM 只能生成 `pending_review` draft/amendment。
- Web search 必须 HTTPS、allowlist、redaction、timeout、audit、metric、degraded。
- Tempo discovery 生产不 auto-publish。
- Grafana ingest 启用时必须 HMAC。
- External embedding 需要 safe URL 和 scope。

## 回滚

基础回滚：

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

回滚后检查：

- `/readyz` ready。
- Smoke eval 通过。
- L2/L3 审批路径正常。
- `agentp_m9_feature_enabled` 未启用功能为 0。
- `agentp_m9_feature_flag_conflict_total` 无新增。

## 禁止项

- 不默认开启 `EXECUTOR_BACKEND=live`。
- 不执行真实数据删除、truncate、flush cache、modify database。
- 不把真实 LLM/full eval 作为 CI 稳定门禁。
- 不记录 raw secret、完整 prompt、认证 header 或私钥。
- 不让 M9 子功能绕过 `M9_EXTENSIONS_ENABLED`。
