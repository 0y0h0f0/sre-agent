# 仓库地图

**最后更新：** 2026-06-13

本文帮助开发者从目录结构定位代码、文档、测试和 demo 数据。统计不包含 `__pycache__`、`node_modules`、`dist`、`coverage`、`.vite-cache`、`test-results` 等生成物。

## 顶层结构

```text
agentp/
  apps/           应用入口：FastAPI、Celery worker、React 控制台
  packages/       共享库：agent、tools、rag、memory、db、discovery、common、evals
  tests/          后端单元/集成/契约/E2E/manual 测试
  migrations/     Alembic 数据库迁移
  deploy/         Docker Compose、Prometheus/Loki/OTel/Grafana、K8s manifest
  demo/           告警 fixture、故障 fixture、runbook、demo service、拓扑
  docs/           当前读者文档，描述已实现行为
  plans/          历史实现规划和 post-MVP roadmap 背景
```

| 目录 | 当前规模 | 说明 |
|------|----------|------|
| `apps/` | 约 60 个 Python 文件 + 前端文件 | API、worker、web 三个应用入口 |
| `packages/` | 约 161 个 Python 文件 | 主要业务库和可测试模块 |
| `tests/` | 约 102 个 Python 测试文件 | 后端 unit/integration/e2e/contract/manual |
| `migrations/versions/` | 15 个迁移版本 | 从初始 schema 到 M9 相关表/字段 |
| `deploy/` | 约 31 个文件 | 本地 compose 支撑文件和 Kubernetes manifest |
| `demo/` | 约 24 个文件 | 4 个 alert fixture、4 个 fault fixture、12 个 runbook |
| `docs/` | 约 40 个 Markdown 文件 | 当前文档源 |
| `plans/` | 约 33 个 Markdown 文件 | 历史计划和 roadmap 背景 |

## apps/ 应用层

| 目录 | 当前规模 | 职责 |
|------|----------|------|
| `apps/api/` | 14 个 router、17 个 schema、14 个 service | FastAPI app、middleware、auth、rate limit、WebSocket、request/response schema、业务服务 |
| `apps/worker/` | 4 个 Python 文件 | Celery app、diagnosis/discovery/poll/eval task 入口 |
| `apps/web/` | Vite/React 项目 | React 控制台、API client、Vitest、Playwright、Docker/nginx 文件 |

### `apps/api/`

```text
apps/api/
  main.py                 FastAPI app factory, middleware, router registration
  dependencies.py         DB/session/settings/task/current-key dependencies
  rate_limit.py           API rate limit helper
  middleware/auth.py      API key auth middleware
  routers/                HTTP API router modules
  schemas/                Pydantic request/response schemas
  services/               Business logic and transaction orchestration
  ws/                     Incident WebSocket publisher/router
```

Router 应保持薄层。新增 API 通常需要同时修改 `routers/`、`schemas/`、`services/`、`packages/db/repositories/`、API 文档和测试。

### `apps/worker/`

```text
apps/worker/
  celery_app.py           Celery app configuration
  tasks.py                diagnosis/resume/discovery/poll/eval task definitions
  main.py                 worker process entry helper
```

Worker 是 LangGraph 的运行入口。FastAPI 只入队任务，不内联执行诊断。

### `apps/web/`

```text
apps/web/
  src/App.tsx             React console routes/views
  src/api.ts              API client helpers
  src/*.test.tsx          Vitest tests
  src/e2e/smoke.spec.ts   Playwright smoke path
  package.json            npm scripts and frontend dependencies
```

前端页面的详细行为见 [React 控制台](../06-frontend/react-console.md)。

## packages/ 共享库层

| 包 | 当前规模 | 职责 |
|----|----------|------|
| `packages/agent/` | 44 个 Python 文件 | LangGraph graph、18 个节点、runner、state、schemas、FakeLLM、LLM adapters、guardrail |
| `packages/common/` | 11 个 Python 文件 | Settings、errors、metrics、redaction、backend auth、URL safety、time helpers |
| `packages/db/` | `models.py` + session + 26 个 repository | SQLAlchemy models、repository、DB session |
| `packages/discovery/` | 25 个 Python 文件 + templates | 后端发现、能力评估、配置 proposal/publish/merge、PromQL/LogQL/K8s/Grafana/Tempo 支撑 |
| `packages/tools/` | 15 个 Python 文件 | Metrics/logs/traces/deployment/K8s/DB/runbook/executor 工具和后端 |
| `packages/rag/` | 20 个 Python 文件 | Runbook ingest/split/metadata/embedding/search/rerank/generation/web/diff |
| `packages/memory/` | 7 个 Python 文件 | Context budget、context builder、deterministic compression、memory store、token count |
| `packages/evals/` | 8 个 Python 文件 | Eval runner、harness、replay、shadow、datasets |

## tests/ 测试层

| 目录 | 当前规模 | 主要覆盖 |
|------|----------|----------|
| `tests/unit/` | 71 个 Python 文件 | 纯函数、service、tool、guardrail、settings、RAG/memory 单元行为 |
| `tests/integration/` | 22 个 Python 文件 | API/DB/Celery/checkpoint/approval/report/discovery 等跨模块路径 |
| `tests/e2e/` | 5 个 Python 文件 | 端到端 smoke、M9 gated path、Tempo/Grafana/semantic search 等 |
| `tests/contract/` | 1 个 Python 文件 | API contract 稳定性 |
| `tests/manual/` | 2 个 Python 文件 | 手动 full eval 或真实 provider 相关验证 |

测试策略见 [测试策略](../07-testing/testing-strategy.md)。CI 和 smoke eval 必须保持 FakeLLM 与 fixture executor。

## deploy/ 基础设施

```text
deploy/
  prometheus.yml          Prometheus scrape 配置
  loki.yml                Loki local config
  promtail.yml            Promtail config
  otel-collector.yml      OTel collector config
  bge-zh.Dockerfile       本地 BGE-ZH embedding 服务镜像
  bge_zh_server.py        BGE-ZH 服务入口
  grafana/                dashboards 和 datasources provisioning
  k8s/                    Kubernetes base/production overlay manifest
```

默认 compose 服务为 postgres、redis、prometheus、loki、promtail、otel-collector、bge-zh、grafana、api、worker、beat、web、demo-service。`mailpit` 位于 `dev` profile。

## demo/ Fixture 与演示数据

```text
demo/
  alerts/                 4 个告警 fixture
  faults/                 trace/git/k8s/db_diagnostics fixture
  runbooks/               12 个 Markdown runbook，按 service/fault_type 分组
  topology.json           服务依赖图，用于级联故障分析
  demo_service/           演示 checkout API
```

当前确定性 demo 覆盖原始 4 类 incident fixture：数据库连接耗尽、高 5xx after deploy、Redis cache avalanche、pod restart loop。FakeLLM 和规则 fallback 还覆盖更多扩展 fault classes；具体诊断逻辑见 `packages/agent/fake_llm.py` 和 `packages/agent/rules_fallback.py`。

## docs/ 与 plans/

```text
docs/                    当前读者文档，优先描述已实现行为
  00-overview/           总览、架构、边界、快速开始、仓库地图、开发者指南
  01-backend/            API、数据模型、后端架构、认证、Celery、错误
  02-agent/              LangGraph 工作流、guardrail/approval、LLM/prompt
  03-tools/              工具层
  04-rag/                Runbook RAG
  05-memory/             Memory/cache/compression
  06-frontend/           React console
  07-testing/            测试策略
  08-deploy/             本地演示
  09-evals/              评估
  10-operations/         开发、运维、demo playbook
  11-reference/          配置、状态/ID、术语
plans/                   历史实施计划和 roadmap 背景
```

判断冲突时按 [开发者全景指南](developer-guide.md) 的源文档优先级处理：当前代码与迁移、当前 `docs/`、`AGENTS.md`、仍适用的 codegen 检查清单、历史 `plans/`。

## 常见定位路径

| 任务 | 先看代码 | 再看文档 |
|------|----------|----------|
| 新增 API | `apps/api/routers`、`schemas`、`services`、`packages/db/repositories` | `docs/01-backend/api-reference.md` |
| 修改 Agent 节点 | `packages/agent/graph.py`、`packages/agent/nodes`、`packages/agent/state.py` | `docs/02-agent/workflow.md` |
| 调整风险规则 | `packages/agent/guardrails/policy.py`、approval service/node | `docs/02-agent/guardrails-and-approval.md` |
| 新增工具后端 | `packages/tools` | `docs/03-tools/tool-layer.md` |
| 修改 runbook 搜索 | `packages/rag`、runbook repositories | `docs/04-rag/runbook-rag.md` |
| 修改上下文压缩 | `packages/memory`、`packages/agent/nodes/build_context.py` | `docs/05-memory/memory-cache-compression.md` |
| 修改配置 | `packages/common/settings.py`、`feature_flags.py` | `docs/11-reference/configuration.md` |
| 修改本地部署 | `docker-compose.yml`、`deploy/`、`demo/` | `docs/08-deploy/local-demo.md` |
| 修改前端 | `apps/web/src` | `docs/06-frontend/react-console.md` |
