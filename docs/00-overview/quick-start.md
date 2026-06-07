# 快速开始

## 前置条件

- Python 3.11+
- Node.js 22 或兼容版本
- Docker 与 Docker Compose
- 可选：PostgreSQL/Redis 本地实例。如果使用 Docker Compose，不需要单独安装。

## 启动完整本地栈

```bash
docker compose up -d
```

启动后常用地址：

| 服务 | 地址 |
| --- | --- |
| API | `http://localhost:8000` |
| API health | `http://localhost:8000/healthz` |
| API docs | `http://localhost:8000/docs` |
| Web | `http://localhost:5173` |
| Demo service | `http://localhost:8080` |
| Prometheus | `http://localhost:9090` |
| Loki | `http://localhost:3100` |
| Grafana | `http://localhost:3000` |

Compose 中 PostgreSQL 暴露为 `localhost:5433`，Redis 暴露为 `localhost:6378`。

## 本地开发方式

先启动依赖：

```bash
docker compose up -d postgres redis prometheus loki otel-collector
```

安装后端依赖：

```bash
python -m pip install -e ".[dev]"
```

执行迁移：

```bash
alembic upgrade head
```

启动 API：

```bash
uvicorn apps.api.main:app --reload --port 8000
```

启动 worker：

```bash
celery -A apps.worker.tasks:celery_app worker --loglevel=INFO
```

启动前端：

```bash
cd apps/web
npm install
npm run dev
```

## 发送 demo 告警

示例：

```bash
curl -X POST http://localhost:8000/api/alerts \
  -H "Content-Type: application/json" \
  -H "X-Request-Id: req_demo_high_5xx" \
  --data @demo/alerts/high-5xx.json
```

返回会包含：

- `incident_id`
- `agent_run_id`
- `celery_task_id`
- `status`
- `deduplicated`

随后可在 Web 的 `/incidents` 页面查看事故，或使用 API 查询：

```bash
curl http://localhost:8000/api/incidents
curl http://localhost:8000/api/agent-runs/<agent_run_id>
```

## Runbook 入库

```bash
curl -X POST http://localhost:8000/api/runbooks/ingest \
  -H "Content-Type: application/json" \
  -H "X-Request-Id: req_ingest_runbooks" \
  -d '{"path":"demo/runbooks","reingest":true}'
```

搜索：

```bash
curl "http://localhost:8000/api/runbooks/search?q=5xx%20after%20deploy&service=checkout-api&top_k=5"
```

## 常用测试命令

后端：

```bash
pytest tests/unit tests/integration --cov=apps --cov=packages --cov-report=term-missing --cov-fail-under=80
ruff check apps packages tests
mypy apps packages
```

前端：

```bash
cd apps/web
npm run test
npm run test:coverage
npm run test:e2e
npm run build
```

评测：

```bash
python -m packages.evals.runner --suite smoke
python -m packages.evals.runner --suite full --output reports/eval-full.json
```

## API Key 鉴权注意

当前配置默认 `API_KEY_AUTH_ENABLED=true`，开放路径包括 `/healthz`、`/readyz`、`/metrics`、`/docs`、`/openapi.json`。如果访问业务 API 返回鉴权错误，需要配置初始 key、创建 API key，或在本地 demo 中显式关闭鉴权。

本地临时关闭可在环境变量中设置：

```bash
API_KEY_AUTH_ENABLED=false
```

生产式使用不应关闭鉴权。
