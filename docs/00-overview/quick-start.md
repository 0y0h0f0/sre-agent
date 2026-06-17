# 快速开始

**最后更新：** 2026-06-14

本页给出从空仓库到本地跑通 demo 的最短路径。完整服务拓扑见 [本地部署与演示环境](../08-deploy/local-demo.md)，演示脚本见 [演示操作手册](../10-operations/demo-playbook.md)。

## 前置条件

- Python 3.11+。
- Node.js 22+ 和 npm。
- Docker / Docker Compose。
- 可选：本地已准备 `models/bge-small-zh/`。默认 `EMBEDDING_PROVIDER=fake`，没有模型也不影响默认诊断路径。

## 推荐路径：完整 Docker Compose

默认本地 demo 使用 FakeLLM、fixture executor、fixture trace/git/k8s/db 诊断后端，安全且可重复。

```bash
docker compose up -d
```

这会启动 13 个默认服务：PostgreSQL、Redis、Prometheus、Loki、Promtail、OTel Collector、BGE-ZH、Grafana、demo-service、web、api、worker、beat。`api` 容器启动命令会先执行 `alembic upgrade head`，通常不需要额外手动迁移。

需要本地邮件 UI 时启用 dev profile：

```bash
docker compose --profile dev up -d
```

`mailpit` 是 dev profile 服务，不计入默认 13 个服务。

## 端口

| 服务 | 宿主机 URL/端口 | 说明 |
|------|-----------------|------|
| React 控制台 | `http://localhost:5173` | 事件、诊断运行、审批、报告 |
| API | `http://localhost:8000` | FastAPI |
| Demo service | `http://localhost:8080` | 故障注入和 `/metrics` |
| Grafana | `http://localhost:3000` | 匿名 Viewer |
| Prometheus | `http://localhost:9090` | metrics 查询 |
| Loki | `http://localhost:3100` | logs 查询 |
| OTel Collector | `4317` / `4318` | OTLP gRPC / HTTP |
| PostgreSQL | `localhost:5433` | 容器内是 `postgres:5432` |
| Redis | `localhost:6378` | 容器内是 `redis:6379` |
| BGE-ZH | `http://localhost:8083` | 仅在切换到 bge embedding 时需要 |
| Mailpit | `http://localhost:8025` / SMTP `1025` | 仅 `--profile dev` |

## 健康检查

```bash
curl http://localhost:8000/healthz
curl http://localhost:8000/readyz
curl http://localhost:9090/-/healthy
curl http://localhost:3000/api/health
curl http://localhost:8080/healthz
```

## 录入 Runbook

默认 demo runbook 位于 `demo/runbooks/checkout-api/`，共 12 个 markdown 文件。录入后，Agent 诊断会在 RAG 阶段引用 chunk id 和 source path。

```bash
curl -X POST http://localhost:8000/api/runbooks/ingest   -H "Content-Type: application/json"   -d '{"path":"demo/runbooks","reingest":true}'
```

如果开启了 API key 认证，给请求加上：

```bash
-H "Authorization: Bearer <api_key>"
```

Compose 默认 `API_KEY_AUTH_ENABLED=false`，首次本地 demo 不需要 token。

## 触发一个事件

可以先让 demo service 写入对应 metrics/logs，再提交告警 fixture：

```bash
curl -X POST http://localhost:8080/faults/high-5xx-after-deploy

curl -X POST http://localhost:8000/api/alerts   -H "Content-Type: application/json"   -d @demo/alerts/high-5xx.json
```

然后打开 `http://localhost:5173/incidents`：

1. 进入新事件详情。
2. 打开最近的 Agent Run，观察节点轨迹、工具调用、cache/token/compression 展示。
3. 如果动作进入 `waiting_approval`，到 `/approvals` 审批。
4. 打开事件报告页查看最终报告。

其他内置告警 fixture：

| 文件 | `alert_name` | 典型关注点 |
|------|--------------|------------|
| `demo/alerts/high-5xx.json` | `High5xxAfterDeploy` | 部署后 5xx，常见 L3 rollback 审批 |
| `demo/alerts/cache-avalanche.json` | `RedisCacheAvalanche` | 缓存命中率下降、DB 压力上升 |
| `demo/alerts/db-connection-exhaustion.json` | `DatabaseConnectionExhaustion` | DB 连接池耗尽和慢查询证据 |
| `demo/alerts/pod-restart-loop.json` | `PodRestartLoop` | K8s 事件、OOMKilled、重启循环 |

FakeLLM 还覆盖 CPU throttling、memory leak、disk full、certificate expiry、DNS failure、message queue lag、rate limit、slow API、error budget burn、P0 outage、downstream timeout 等告警名称。仓库只提供上表 4 个可直接提交的 fixture；扩展故障类可以通过自定义 `alert_name` payload 验证，未知名称会回退到 high-5xx 诊断路径。

## 手动开发模式

如果要在宿主机运行 API、worker 或前端，只让 Docker 提供依赖服务，可以先启动依赖：

```bash
docker compose up -d postgres redis prometheus loki promtail otel-collector grafana demo-service
```

如果使用 Compose 映射出的 PostgreSQL/Redis，宿主机进程需要使用映射端口，而不是容器内端口：

```bash
export DATABASE_URL=postgresql+psycopg://sre:sre@localhost:5433/sre
export REDIS_URL=redis://localhost:6378/0
export CELERY_BROKER_URL=redis://localhost:6378/1
export CELERY_RESULT_BACKEND=redis://localhost:6378/2
export PROMETHEUS_URL=http://localhost:9090
export LOKI_URL=http://localhost:3100
export OTEL_COLLECTOR_URL=http://localhost:4318
export LLM_PROVIDER=fake
export EMBEDDING_PROVIDER=fake
export RERANKER_PROVIDER=fake
export TRACE_BACKEND=fixture
export DEPLOYMENT_BACKEND=fixture
export K8S_BACKEND=fixture
export DB_DIAGNOSTICS_BACKEND=fixture
export EXECUTOR_BACKEND=fixture
export API_KEY_AUTH_ENABLED=false
```

`.env.example` 里的 `localhost:5432` 和 `localhost:6379` 适合你自己在宿主机运行 PostgreSQL/Redis 的情况；如果依赖来自 Compose，请改成 `5433` 和 `6378`。

启动本地进程：

```bash
alembic upgrade head
uvicorn apps.api.main:app --reload --port 8000
```

另开终端：

```bash
celery -A apps.worker.tasks:celery_app worker --loglevel=INFO
```

另开前端终端：

```bash
cd apps/web
npm ci
VITE_API_BASE_URL=http://localhost:8000 npm run dev
```

## 常用开发命令

```bash
python -m pip install -e ".[dev]"
pytest tests/unit tests/integration --cov=apps --cov=packages --cov-report=term-missing --cov-report=xml --cov-fail-under=80
ruff check apps packages tests
mypy apps packages
```

前端命令在 `apps/web/` 下执行：

```bash
npm run dev
npm run build
npm run test:coverage
npm run test:e2e
```

## 默认安全配置

| 配置 | 默认 local/demo 行为 |
|------|----------------------|
| `LLM_PROVIDER` | `fake` |
| `EXECUTOR_BACKEND` | `fixture` |
| `TRACE_BACKEND` | Compose 为 `fixture`；`.env.example` 为 disabled/fixture path |
| `DEPLOYMENT_BACKEND` | `fixture` |
| `K8S_BACKEND` | `fixture`，诊断只读 |
| `DB_DIAGNOSTICS_BACKEND` | `fixture`，live 模式也只允许预定义 SELECT |
| `API_KEY_AUTH_ENABLED` | Compose 默认 `false` |
| `M9_EXTENSIONS_ENABLED` | `false` |

不要为了本地 demo 开启 `EXECUTOR_BACKEND=live`。live executor 是显式 operator opt-in，只允许 restart/pause/resume/scale/rollback 受控 Kubernetes mutation，并且仍要经过 guardrail 和审批。

## 故障排查

| 现象 | 检查 |
|------|------|
| API 无法连 DB | Compose 内部用 `postgres:5432`，宿主机进程用 `localhost:5433` |
| Worker 不消费任务 | 检查 `CELERY_BROKER_URL` 是否和 API 指向同一个 Redis DB 1 |
| 前端 401 | 左侧认证面板设置 API key，或确认 `API_KEY_AUTH_ENABLED=false` |
| 前端连不到 API | 宿主机运行前端时设置 `VITE_API_BASE_URL=http://localhost:8000` |
| 没有 runbook 证据 | 先调用 `/api/runbooks/ingest`，并确认 runbook chunk 已入库 |
| BGE-ZH 容器报模型错误 | 默认 fake embedding 不依赖它；只有切换 `EMBEDDING_PROVIDER=bge_zh` 时必须准备模型 |

## 下一步

- 阅读 [架构](architecture.md) 理解端到端数据流。
- 阅读 [React 控制台](../06-frontend/react-console.md) 定位页面数据来源。
- 阅读 [Agent 工作流](../02-agent/workflow.md) 理解诊断节点和 resume。
- 阅读 [API 参考](../01-backend/api-reference.md) 查找端点契约。
