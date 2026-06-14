# 本地部署与演示环境

**最后更新：** 2026-06-14

本页描述 `docker-compose.yml` 当前本地 demo 拓扑。默认路径是安全演示环境：FakeLLM、fixture executor、fixture 诊断后端，API 入队到 Celery，worker 执行 LangGraph，前端通过 API 和 WebSocket 展示事件进度。

## Compose 服务

默认 `docker compose up -d` 启动 13 个服务；`mailpit` 只在 `dev` profile 启动。

| 服务 | 镜像/构建 | 宿主机端口 | Profile | 说明 |
|------|-----------|------------|---------|------|
| `postgres` | `pgvector/pgvector:pg16` | `5433 -> 5432` | 默认 | 主数据库、pgvector、LangGraph checkpoint 存储 |
| `redis` | `redis:7-alpine` | `6378 -> 6379` | 默认 | Celery broker/result、缓存、pubsub |
| `prometheus` | `prom/prometheus:v2.55.1` | `9090` | 默认 | 抓取 API 和 demo-service metrics |
| `loki` | `grafana/loki:3.3.2` | `3100` | 默认 | 日志聚合 |
| `promtail` | `grafana/promtail:3.3.2` | - | 默认 | 读取 Docker 日志并转发 Loki |
| `otel-collector` | `otel/opentelemetry-collector:0.114.0` | `4317`, `4318` | 默认 | OTLP gRPC/HTTP 收集 |
| `bge-zh` | `deploy/bge-zh.Dockerfile` | `8083` | 默认 | 本地 BAAI/bge-small-zh embedding 服务，默认 fake embedding 不依赖它 |
| `grafana` | `grafana/grafana:11.3.1` | `3000` | 默认 | 匿名 Viewer，预配置 Prometheus/Loki 数据源和 demo dashboard |
| `demo-service` | 仓库镜像 | `8080` | 默认 | FastAPI 演示目标服务，提供 fault injection 和 `/metrics` |
| `web` | `node:22-alpine` | `5173` | 默认 | React 控制台，容器内执行 `npm install && npm run dev` |
| `api` | 仓库镜像 | `8000` | 默认 | FastAPI API，启动时先执行 `alembic upgrade head` |
| `worker` | 仓库镜像 | - | 默认 | Celery worker，执行诊断、resume、discovery/poll/eval 任务 |
| `beat` | 仓库镜像 | - | 默认 | Celery Beat 调度任务，保持单实例 |
| `mailpit` | `axllent/mailpit:v1.22` | `8025`, `1025` | `dev` | 本地 SMTP 测试 UI |

## 启动与停止

完整 demo：

```bash
docker compose up -d
```

带邮件测试 UI：

```bash
docker compose --profile dev up -d
```

查看状态和日志：

```bash
docker compose ps
docker compose logs -f api worker web
```

API 容器的 command 是：

```bash
alembic upgrade head && uvicorn apps.api.main:app --host 0.0.0.0 --port 8000 --reload
```

因此完整 Compose 路径通常不需要再从宿主机执行迁移。手动开发模式见 [快速开始](../00-overview/quick-start.md)。

## 网络与端口口径

Compose 内部服务通过服务名通信：

- API/worker 连接 PostgreSQL：`postgres:5432`。
- API/worker 连接 Redis：`redis:6379`。
- API/worker 连接 Prometheus/Loki/OTel：`prometheus:9090`、`loki:3100`、`otel-collector:4318`。
- 前端容器的 Vite proxy 将 `/api` 转到 `http://api:8000`。

宿主机进程连接 Compose 依赖时要使用映射端口：PostgreSQL `localhost:5433`，Redis `localhost:6378`。这是 `.env.example` 默认 `5432/6379` 最容易出错的地方。

## Demo 数据

| 目录/文件 | 内容 |
|-----------|------|
| `demo/alerts/high-5xx.json` | `High5xxAfterDeploy` 告警 fixture |
| `demo/alerts/cache-avalanche.json` | `RedisCacheAvalanche` 告警 fixture |
| `demo/alerts/db-connection-exhaustion.json` | `DatabaseConnectionExhaustion` 告警 fixture |
| `demo/alerts/pod-restart-loop.json` | `PodRestartLoop` 告警 fixture |
| `demo/faults/traces.json` | Trace fixture |
| `demo/faults/git_changes.json` | 部署/变更 fixture |
| `demo/faults/k8s.json` | Kubernetes 诊断 fixture |
| `demo/faults/db_diagnostics.json` | DB 诊断 fixture |
| `demo/runbooks/checkout-api/` | 4 个场景目录、12 个 markdown runbook |
| `demo/demo_service/main.py` | 演示服务 fault injection 和 Prometheus metrics |

仓库提供 4 个现成告警 payload。FakeLLM 的确定性覆盖面更广，支持更多 `alert_name`，但扩展类没有一一提供 JSON fixture。

## Demo Service

`demo-service` 是端口 `8080` 的 FastAPI 服务。它暴露：

| Endpoint | 行为 |
|----------|------|
| `GET /healthz` | 健康检查 |
| `GET /metrics` | Prometheus metrics |
| `POST /faults/high-5xx-after-deploy` | 增加 500 请求、延迟观测值，并输出部署后 5xx 日志 |
| `POST /faults/cache-avalanche` | 降低 Redis 命中率、提高 DB 连接数，并输出缓存雪崩日志 |
| `POST /faults/db-connection-exhaustion` | 将 DB active connections 设为 96，并输出连接池耗尽日志 |
| `POST /faults/pod-restart-loop` | 增加 pod restart counter、设置高内存，并输出 OOMKilled 日志 |
| `POST /faults/clear` | 清理 fault 状态，恢复基础 metrics |

这些 endpoint 只影响 demo-service 的内存指标和日志，不会修改真实业务数据库或外部系统。

## Observability 配置

- `deploy/prometheus.yml` 每 5 秒抓取 `prometheus:9090`、`api:8000/metrics` 和 `demo-service:8080/metrics`。
- `deploy/loki.yml` 是本地 Loki 配置，数据写入 `loki-data` volume。
- `deploy/promtail.yml` 通过只读挂载 Docker socket 收集容器日志。
- `deploy/otel-collector.yml` 提供 OTLP 接收端，供 demo-service 和后续 trace adapter 使用。
- Grafana provisioning 位于 `deploy/grafana/datasources/` 和 `deploy/grafana/dashboards/`。

Grafana 地址：`http://localhost:3000`，匿名 Viewer 已启用。预置 dashboard 文件是 `deploy/grafana/dashboards/sre-demo-service.json`。

## 默认环境变量

`api`、`worker`、`beat` 在 Compose 中共享主要运行时配置：

| 分类 | 默认值/行为 |
|------|-------------|
| LLM | `LLM_PROVIDER=fake`，`LLM_MODEL=fake-diagnosis-model` |
| Embedding/Reranker | `EMBEDDING_PROVIDER=fake`，`RERANKER_PROVIDER=fake` |
| Trace | `TRACE_ENABLED=true`，`TRACE_BACKEND=fixture`，`TRACE_FIXTURE_PATH=demo/faults/traces.json` |
| Deployment | `DEPLOYMENT_BACKEND=fixture`，`GIT_CHANGES_FIXTURE_PATH=demo/faults/git_changes.json` |
| Kubernetes diagnostics | `K8S_BACKEND=fixture`，`K8S_FIXTURE_PATH=demo/faults/k8s.json` |
| DB diagnostics | `DB_DIAGNOSTICS_BACKEND=fixture`，`DB_DIAGNOSTICS_FIXTURE_PATH=demo/faults/db_diagnostics.json` |
| Executor | `EXECUTOR_BACKEND=fixture` |
| Auth | `API_KEY_AUTH_ENABLED=false` |
| CORS | `CORS_ALLOW_ORIGINS=http://localhost:5173` |
| Discovery | `DISCOVERY_ENABLED=true`，`DISCOVERY_APPLY_MODE=inherit` |

M9 相关 feature flag 在 `.env.example` 中默认全部关闭。不要在本地 demo 文档或脚本中把 M9 能力写成默认必需能力。

## 运行主路径

1. 启动 Compose。
2. 调用 `/api/runbooks/ingest` 录入 `demo/runbooks`。
3. 可选：调用 demo-service fault endpoint 产生 metrics/logs。
4. `POST /api/alerts` 提交 `demo/alerts/*.json`。
5. API 创建/去重 incident，创建 agent run，并把诊断任务入队 Celery。
6. Worker 运行 LangGraph，读取 Prometheus/Loki/fixture tools/RAG/memory。
7. L2/L3 action 停在审批；L4 直接拒绝。
8. 前端在事件页、Agent Run 页、审批页和报告页展示结果。

## 扩缩容

API 和 worker 可以水平扩展：

```bash
docker compose up -d --scale api=3
docker compose up -d --scale worker=3
```

`beat` 必须保持单实例，避免重复调度。扩容不会改变默认 fixture executor 和审批边界。

## 安全边界

- 默认 executor 是 `fixture`，不会真实修改 Kubernetes 或云资源。
- live Kubernetes executor 只能通过 `EXECUTOR_BACKEND=live` 显式选择加入，并且只允许 restart/scale/rollback 三类受控 Deployment mutation。
- live K8s diagnostics 只读；live DB diagnostics 只允许预定义 SELECT。
- L2/L3 必须审批；L3 必须二次确认；L4 直接拒绝。
- 真实 LLM 只用于手动 demo/full eval，不应作为 CI 稳定门禁。

## 常见问题

| 问题 | 处理 |
|------|------|
| `web` 容器启动慢 | 首次启动会执行 `npm install`，观察 `docker compose logs -f web` |
| API readyz 失败 | 检查 postgres/redis healthcheck，或看 `docker compose logs api` |
| 提交告警后无 run | 检查 worker 日志和 Redis broker URL |
| 前端没有实时节点 | Agent Run 页面仍会 5 秒轮询；检查 WebSocket token/auth 和浏览器控制台 |
| 诊断缺少 runbook | 先执行 runbook ingest，并确认 `runbook_chunks` 有数据 |
| BGE-ZH 容器不可用 | 默认 fake embedding 不依赖它；只有切换 provider 时需要修复模型路径 |
