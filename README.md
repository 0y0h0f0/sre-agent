# SRE 事件响应 Agent

基于 LangGraph 的智能 SRE 事件响应系统——接收告警、自动诊断、生成根因分析，在多层护栏和人工审批下执行受控的修复操作。

---

**状态：** M0–M8 完成 | M9 受控增强已纳入当前文档（生产默认关闭，按特性开关启用）

**核心指标：** 100 个 Python 测试文件 · 15 个数据库迁移 · 默认 13 个 Compose 服务（mailpit 为 dev profile）· 14 个 API router / 76 个 HTTP route · 32 个数据模型

---

## 快速开始

最短本地 demo 路径：

```bash
# 启动完整本地技术栈；api 容器会先执行 alembic upgrade head
docker compose up -d

# 健康检查
curl http://localhost:8000/healthz
curl http://localhost:8000/readyz

# 录入 demo runbook
curl -X POST http://localhost:8000/api/runbooks/ingest \
  -H "Content-Type: application/json" \
  -d '{"path":"demo/runbooks","reingest":true}'

# 注入一个 demo fault 并提交告警
curl -X POST http://localhost:8080/faults/high-5xx-after-deploy
curl -X POST http://localhost:8000/api/alerts \
  -H "Content-Type: application/json" \
  -d @demo/alerts/high-5xx.json
```

打开 React 控制台：`http://localhost:5173`。

邮件测试 UI 需要 dev profile：

```bash
docker compose --profile dev up -d
```

手动运行 API/worker/frontend 的开发模式、宿主机端口映射和环境变量差异见 [快速开始](docs/00-overview/quick-start.md)。

## 系统架构

```
告警 (Webhook / Alertmanager Poll)
        │
        ▼
  FastAPI (apps/api)
        │
        ▼
  Celery Worker ──→ LangGraph 诊断工作流（18 个节点）
        │
        ├─ Metrics (Prometheus)
        ├─ Logs   (Loki)
        ├─ Traces (Jaeger / Tempo)
        ├─ K8s    (read-only)
        ├─ DB     (read-only PostgreSQL)
        ├─ Git    (GitHub / Argo CD)
        └─ Runbook RAG (pgvector)
        │
        ▼
  根因分析 → 护栏检查 → 审批 (L2/L3) → 执行 → 验证 → 报告
```

### Docker 服务（默认 13 个，dev profile 可选 mailpit）

| 服务 | 镜像 | 端口 | 用途 |
|------|------|------|------|
| postgres | pgvector/pgvector:pg16 | 5433 | 主数据库 + 向量存储 |
| redis | redis:7-alpine | 6378 | 消息队列 / 缓存 |
| prometheus | prom/prometheus:v2.55.1 | 9090 | 指标采集 |
| loki | grafana/loki:3.3.2 | 3100 | 日志聚合 |
| grafana | grafana/grafana:11.3.1 | 3000 | 可视化 |
| otel-collector | otel/opentelemetry-collector:0.114.0 | 4317-4318 | 链路追踪收集 |
| promtail | grafana/promtail:3.3.2 | — | 日志采集代理 |
| bge-zh | BAAI/bge-small-zh | 8083 | 中文 Embedding 服务 |
| mailpit | axllent/mailpit:v1.22 | 8025, 1025 | 邮件测试（dev profile） |
| demo-service | FastAPI | 8080 | 演示目标服务 |
| web | node:22-alpine | 5173 | React 控制台 |
| api | FastAPI | 8000 | 后端 API |
| worker | Celery | — | 异步诊断 |
| beat | Celery Beat | — | 定时任务调度 |

## 技术栈

| 层 | 技术 | 说明 |
|----|------|------|
| **后端** | Python 3.11+ · FastAPI · Pydantic · SQLAlchemy · Alembic | REST API + 数据模型 |
| **Agent** | LangGraph · LangChain | 有状态诊断工作流，PostgreSQL 持久化检查点 |
| **异步任务** | Celery · Redis | 诊断任务调度，Celery Beat 定时任务 |
| **数据库** | PostgreSQL 16 · pgvector | 关系数据 + 向量相似搜索 |
| **前端** | React 19 · TypeScript 5.7 · Vite 6 · React Router 7 · TanStack Query 5 | SPA 控制台 |
| **测试** | pytest · pytest-cov · Vitest · Playwright | CI 跑 unit/integration、smoke eval、前端 coverage/build/E2E，覆盖率硬门禁 ≥ 80% |

## M9 受控增强

M9 在 M0–M8 确定性诊断基础上，新增 AI、Web 搜索、Tempo、Grafana 和语义搜索能力——**所有功能在生产环境默认关闭**，由 `M9_EXTENSIONS_ENABLED` 统一控制。

| PR | 能力 | 开关 |
|----|------|------|
| 9.1 | M9 全局特性门 | `M9_EXTENSIONS_ENABLED` |
| 9.2 | LLM Runbook 草稿生成 | `RUNBOOK_LLM_GENERATION_ENABLED` |
| 9.3 | LLM 事件差异分析 | `LLM_INCIDENT_DIFF_ENABLED` |
| 9.4 | Web 搜索安全 | `RUNBOOK_WEB_SEARCH_ENABLED` |
| 9.5 | Tempo Trace 后端 | `TRACE_BACKEND=tempo` |
| 9.6 | Tempo 自动发现 | `TEMPO_DISCOVERY_ENABLED` |
| 9.7 | Grafana 告警 Webhook | `GRAFANA_ALERT_INGEST_ENABLED` |
| 9.8 | 语义 Runbook 搜索 | `SEMANTIC_RUNBOOK_SEARCH_ENABLED` |
| 9.9 | 外部 Embedding 提供商 | `EXTERNAL_EMBEDDING_PROVIDER_ENABLED` |

### M9 核心不变量

- **LLM 仅生成草稿**（`RunbookDraft` / `AmendmentDraft`，状态 `pending_review`）。绝不自动批准、自动发布、自动执行。
- **生产环境 Tempo 发现绝不自动发布**。
- **Embedding 失败不阻断 Runbook 入库**。
- **所有外部调用**均具备超时、脱敏、审计埋点、指标采集和降级回退。
- 每个 M9 子能力可独立回滚；全局回滚恢复 `PRE_M9_TRACE_BACKEND` / `PRE_M9_TRACE_ENABLED`。

## 项目结构

```
apps/
  api/           FastAPI 应用 — 路由、服务、Pydantic schema
  worker/        Celery 任务 — 诊断、发现、告警轮询、评估
  web/           React + TypeScript + Vite 控制台
packages/
  agent/         LangGraph 工作流（18 个节点）、LLM 适配器、护栏
  common/        配置（100+ 字段）、错误封装、ID 工具、后端认证
  db/            32 个 SQLAlchemy 模型、26 个仓库、会话工厂
  discovery/     后端自动发现、配置合并、生效配置发布
  tools/         工具层（metrics / logs / traces / k8s / db / git / executor / search）
  rag/           Runbook RAG（入库 / 嵌入 / 检索 / 重排序 / 生成）
  memory/        记忆存储、上下文压缩、token 计数
tests/
  unit/          71 个单元测试文件
  integration/   22 个集成测试文件
  e2e/           4 个端到端测试文件
  contract/      1 个契约测试文件
migrations/      15 个 Alembic 版本
deploy/          Docker Compose 配置（Prometheus / Loki / Grafana / OTel / BGE）
demo/            告警 fixture、演示服务、故障数据、runbook 模板
docs/            架构文档、API 参考、运维手册、M9 上线计划
plans/           原始规划文档与里程碑完成记录
```

## 常用命令

```bash
# 运行全部测试 + 覆盖率（≥80%）
pytest tests/unit tests/integration --cov=apps --cov=packages \
  --cov-report=term-missing --cov-fail-under=80

# 运行单个测试文件
pytest tests/unit/test_tools.py -v

# 代码检查
ruff check apps packages tests
mypy apps packages

# 生成数据库迁移
alembic revision --autogenerate -m "description"

# 应用迁移
alembic upgrade head
```

### 前端命令（在 `apps/web/` 下执行）

```bash
npm run dev            # 开发服务器 :5173
npm run build          # 生产构建（tsc + vite）
npm run test           # Vitest 单元测试
npm run test:coverage  # 含覆盖率报告
npm run test:e2e       # Playwright E2E 测试
```

## 关键设计约束

### 环境与安全

- **本地默认安全**：`APP_ENV=local` 使用 FakeLLM、fixture 后端、localhost 默认值，适合开发和 CI。
- **生产环境零信任**：`APP_ENV=production` 默认关闭 LLM（`LLM_PROVIDER=disabled`），使用 fixture executor（`EXECUTOR_BACKEND=fixture`），无 localhost 隐藏回退。
- **原始密钥**使用 `env:VAR_NAME` 引用，绝不落库、不入审计日志、不进 LLM prompt。

### 风险等级与护栏

| 等级 | 描述 | 行为 |
|------|------|------|
| L0 | 只读查询 | 自动执行 |
| L1 | 低风险操作（生成报告、预热缓存） | 自动执行 |
| L2 | 运维操作（重启 Pod、扩缩容） | 需审批 |
| L3 | 回滚/限流 | 需审批 + 二次确认 |
| L4 | 破坏性操作（删除数据、清空缓存） | 直接拒绝 |

- 护栏是**确定性规则**，不依赖 LLM 判断。
- L3 审批必须验证 `risk_ack=true` + `confirm_action_type` + `confirm_target`。
- 未知 action 类型保守归类为 L2。

### 配置优先级

```
环境变量 > 活跃覆盖 > 配置模板 > 已发布 EffectiveConfig > 安全默认值
```

- 自动发现仅填补空白，绝不覆盖显式配置。
- Worker **仅读取已发布配置**，绝不访问未发布的提案或 `detected_only` 后端。
- 所有后端 URL 必须通过 `BackendUrlSafetyValidator` 校验（生产环境拒绝 localhost、链路本地地址、元数据端点）。

### 确定性诊断（M0–M8）

- 所有诊断、runbook 模板生成和反馈分析使用确定性方法，不依赖真实 LLM。
- M9 仅在显式开启特性门后增强——绝不替代 M0–M8 的确定性路径。

## 文档索引

| 文档 | 内容 |
|------|------|
| [docs/README.md](docs/README.md) | 文档中心 |
| [docs/00-overview/developer-guide.md](docs/00-overview/developer-guide.md) | 开发者全景指南与阅读路径 |
| [docs/00-overview/documentation-update-plan.md](docs/00-overview/documentation-update-plan.md) | 文档分批更新计划 |
| [docs/00-overview/architecture.md](docs/00-overview/architecture.md) | 系统架构详解 |
| [docs/00-overview/quick-start.md](docs/00-overview/quick-start.md) | 快速开始指南 |
| [docs/00-overview/project-overview.md](docs/00-overview/project-overview.md) | 项目全景 |
| [docs/01-backend/api-reference.md](docs/01-backend/api-reference.md) | API 参考（76 个 HTTP route + 1 个 WebSocket） |
| [docs/01-backend/data-model.md](docs/01-backend/data-model.md) | 数据模型（32 个模型） |
| [docs/01-backend/backend-architecture.md](docs/01-backend/backend-architecture.md) | 后端架构 |
| [docs/02-agent/workflow.md](docs/02-agent/workflow.md) | Agent 工作流（18 个节点） |
| [docs/02-agent/guardrails-and-approval.md](docs/02-agent/guardrails-and-approval.md) | 护栏与审批系统 |
| [docs/03-tools/tool-layer.md](docs/03-tools/tool-layer.md) | 工具层设计 |
| [docs/04-rag/runbook-rag.md](docs/04-rag/runbook-rag.md) | Runbook RAG 系统 |
| [docs/05-memory/memory-cache-compression.md](docs/05-memory/memory-cache-compression.md) | 记忆、缓存与压缩 |
| [docs/06-frontend/react-console.md](docs/06-frontend/react-console.md) | 前端控制台 |
| [docs/11-reference/configuration.md](docs/11-reference/configuration.md) | 配置参考（100+ 字段） |
| [docs/11-reference/glossary.md](docs/11-reference/glossary.md) | 术语表 |
| [docs/08-deploy/local-demo.md](docs/08-deploy/local-demo.md) | 本地部署指南 |
| [docs/production-checklist.md](docs/production-checklist.md) | 生产环境检查清单 |
| [docs/operator-runbook.md](docs/operator-runbook.md) | 运维 Runbook |
| [docs/m9-rollout.md](docs/m9-rollout.md) | M9 上线计划 |
| [CHANGELOG.md](CHANGELOG.md) | 变更日志 |

## 开发规范

- **测试优先**：新功能先写测试，覆盖率达到 80% 以上。
- **代码审查**：所有变更需通过 code-reviewer / security-reviewer 审查。
- **小微模块**：函数 < 50 行，文件 < 800 行，高内聚低耦合。
- **不可变数据**：创建新对象，不修改现有对象。
- **显式错误处理**：不静默吞异常，返回降级结果或结构化错误。
- **实现文档**：参考 `CLAUDE.md`（项目指南）和 `AGENTS.md`（编码规范）。

## 许可证

MIT
