# 本地部署与 Demo 环境

## Docker Compose 服务

`docker-compose.yml` 定义：

| 服务 | 说明 | 端口 |
| --- | --- | --- |
| `postgres` | PostgreSQL + pgvector | `5433:5432` |
| `redis` | queue/cache | `6378:6379` |
| `prometheus` | metrics | `9090` |
| `loki` | logs | `3100` |
| `promtail` | log shipper | 无 |
| `otel-collector` | traces | `4317`、`4318` |
| `grafana` | dashboards | `3000` |
| `demo-service` | fault injection demo service | `8080` |
| `web` | Vite dev server | `5173` |
| `api` | FastAPI | `8000` |
| `worker` | Celery worker | 无 |
| `beat` | Celery beat | 无 |

## 启动

```bash
docker compose up -d
```

查看状态：

```bash
docker compose ps
docker compose logs -f api
docker compose logs -f worker
```

## 迁移

API 容器启动命令会执行：

```bash
alembic upgrade head
```

手动开发时：

```bash
alembic upgrade head
```

当前迁移文件：

- `c26ca1452607_0001_initial_schema.py`
- `0002_phase3_alerts_email.py`
- `0003_runbook_tsvector.py`
- `0004_runbook_drafts_versions.py`
- `0005_runbook_language.py`
- `0006_phase5_feedback.py`
- `0007_phase6_collaboration.py`
- `0008_phase7_api_keys.py`
- `0009_phase7_evals.py`

## 默认环境变量

Compose 中 API/worker/beat 使用：

- `DATABASE_URL=postgresql+psycopg://sre:sre@postgres:5432/sre`
- `REDIS_URL=redis://redis:6379/0`
- `CELERY_BROKER_URL=redis://redis:6379/1`
- `CELERY_RESULT_BACKEND=redis://redis:6379/2`
- `PROMETHEUS_URL=http://prometheus:9090`
- `LOKI_URL=http://loki:3100`
- `OTEL_COLLECTOR_URL=http://otel-collector:4318`
- `TRACE_FIXTURE_PATH=demo/faults/traces.json`
- `GIT_CHANGES_FIXTURE_PATH=demo/faults/git_changes.json`
- `LLM_PROVIDER=fake`
- `EMBEDDING_PROVIDER=fake`

## 健康检查

```bash
curl http://localhost:8000/healthz
curl http://localhost:8000/readyz
curl http://localhost:8000/metrics
```

## 入库 Runbook

```bash
curl -X POST http://localhost:8000/api/runbooks/ingest \
  -H "Content-Type: application/json" \
  -H "X-Request-Id: req_ingest" \
  -d '{"path":"demo/runbooks","reingest":true}'
```

## 发送告警

```bash
curl -X POST http://localhost:8000/api/alerts \
  -H "Content-Type: application/json" \
  -H "X-Request-Id: req_demo" \
  --data @demo/alerts/high-5xx.json
```

若 API key 鉴权开启，增加：

```text
Authorization: Bearer <api_key>
```

## 清理

停止容器：

```bash
docker compose down
```

清理数据卷会删除本地数据：

```bash
docker compose down -v
```

执行清理前应确认不需要保留 demo 数据。

## 扩缩容

Compose 注释中支持：

```bash
docker compose up --scale api=3
docker compose up --scale worker=3
```

本地 demo 可用于验证幂等和 HA 行为，但不是生产部署方案。
