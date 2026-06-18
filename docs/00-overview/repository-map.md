# 仓库地图

**最后更新：** 2026-06-18

本文帮助开发者从目录结构定位代码、文档、测试、demo 数据和文档资产。统计不包含 `__pycache__`、`node_modules`、`dist`、`coverage`、`.vite-cache`、`test-results`、`reports/`、`models/` 等本地生成物。

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
| `apps/` | 约 62 个 Python 文件 + 前端文件 | API、worker、web 三个应用入口 |
| `packages/` | 约 165 个 Python 文件 | 主要业务库和可测试模块 |
| `tests/` | 111 个 `test_*.py` 源文件 | 后端 unit/integration/e2e/contract/manual |
| `migrations/versions/` | 17 个迁移版本 | 从初始 schema 到 M9 相关表/字段 |
| `deploy/` | 约 31 个文件 | 本地 compose 支撑文件和 Kubernetes manifest |
| `demo/` | 约 24 个文件 | 4 个 alert fixture、4 个 fault fixture、12 个 runbook |
| `docs/` | 约 67 个 Markdown 文件 | 当前文档源和文档资产 |
| `plans/` | 约 33 个 Markdown 文件 | 历史计划和 roadmap 背景 |

## 结构边界

源码与长期维护资产保留在 `apps/`、`packages/`、`tests/`、`migrations/`、`deploy/`、`demo/`、`docs/`、`plans/` 和 `templates/`。本地运行生成物不属于项目结构来源：

| 路径 | 归类 | 处理方式 |
|------|------|----------|
| `node_modules/`、`apps/web/node_modules/` | 本地依赖缓存 | 已被 `.gitignore` 忽略。 |
| `coverage.xml`、`apps/web/coverage/`、`apps/web/test-results/` | 测试输出 | 已被 `.gitignore` / `.dockerignore` 忽略。 |
| `reports/` | eval、K8s smoke 和 live executor 验证输出 | 已被 `.gitignore` 忽略；文档只引用输出路径，不提交新报告。 |
| `models/` | 本地下载模型 | 已被 `.gitignore` 忽略。 |
| `celerybeat-schedule*` | Celery Beat 运行时文件 | 已被 `.gitignore` 忽略。 |
| `docs/assets/render-checks/` | 历史渲染检查图片 | 作为文档资产保留；不再放在 `tmp/` 临时目录。 |
| `plan.md`、`tzplan.md`、`realplan.md`、`study.md` | 本地规划/研究材料 | 已被 `.gitignore` 忽略；当前读者文档以 `docs/` 为准。 |

## apps/ 应用层

| 目录 | 当前规模 | 职责 |
|------|----------|------|
| `apps/api/` | 14 个 HTTP router、16 个 schema、15 个 service | FastAPI app、middleware、auth、rate limit、WebSocket、request/response schema、业务服务 |
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

Router 应保持薄层。新增 API 通常需要同时修改 `routers/`、`schemas/`、`services/`、`packages/db/repositories/`、API 文档和测试。需要沿代码路径理解 middleware、request id、API key auth、scope、rate limit、service 事务、Celery 入队、审计和错误信封时，见 [API 控制面与服务层技术深挖](api-control-plane-service-deep-dive.md)。需要理解 `/api/alerts`、Grafana-shaped payload、Alertmanager poll、poll cursor 和 resolved inference 时，见 [Alertmanager Poll、Grafana 与告警来源归一化技术深挖](alert-source-normalization-poll-grafana-deep-dive.md)。

### `apps/worker/`

```text
apps/worker/
  celery_app.py           Celery app configuration
  tasks.py                diagnosis/resume/discovery/poll/eval task definitions
  eval_tasks.py           eval suite async task helper
```

Worker 是 LangGraph 的运行入口。FastAPI 只入队任务，不内联执行诊断。需要沿代码路径理解 Celery task、run 幂等、PostgresSaver、GraphInterrupt resume、node/tool audit、poll/discovery/eval task 时，见 [Worker、Celery 与 LangGraph Checkpoint 技术深挖](worker-celery-langgraph-checkpoint-deep-dive.md)。Alertmanager poll 的 scope、filter hash、cursor 和 resolved inference 细节见 [Alertmanager Poll、Grafana 与告警来源归一化技术深挖](alert-source-normalization-poll-grafana-deep-dive.md)。

### `apps/web/`

```text
apps/web/
  src/App.tsx             React console routes/views
  src/api.ts              API client helpers
  src/*.test.tsx          Vitest tests
  src/e2e/smoke.spec.ts   Playwright smoke path
  package.json            npm scripts and frontend dependencies
```

前端页面的详细行为见 [React 控制台](../06-frontend/react-console.md)。需要沿代码路径理解 API client、TanStack Query、轮询、WebSocket ticket、审批 mutation、报告页、通知和错误状态时，见 [前端控制台与实时更新技术深挖](frontend-realtime-console-deep-dive.md)。

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
| `tests/unit/` | 77 个 `test_*.py` 文件 | 纯函数、service、tool、guardrail、settings、RAG/memory 单元行为 |
| `tests/integration/` | 27 个 `test_*.py` 文件 | API/DB/Celery/checkpoint/approval/report/discovery/工程指标等跨模块路径 |
| `tests/e2e/` | 4 个 `test_*.py` 文件 | 端到端 smoke、M9 gated path、Tempo/Grafana/semantic search 等 |
| `tests/contract/` | 1 个 Python 文件 | API contract 稳定性 |
| `tests/manual/` | 2 个 Python 文件 | 手动 full eval 或真实 provider 相关验证 |

测试策略见 [测试策略](../07-testing/testing-strategy.md)。CI 和 smoke eval 必须保持 FakeLLM 与 fixture executor。

需要沿代码路径理解 pytest/Vitest/Playwright 门禁、测试 fixture 隔离、Eval harness、Eval API/Celery task、工程指标聚合和 CI 调试入口时，见 [测试、Eval 与工程指标技术深挖](testing-eval-engineering-metrics-deep-dive.md)。

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
  08-deploy/             本地演示和 K8s 后端对接验证
  09-evals/              评估
  10-operations/         开发、运维、demo playbook
  11-reference/          配置、后端对接范围、状态/ID、术语
plans/                   历史实施计划和 roadmap 背景
```

判断冲突时按 [开发者全景指南](developer-guide.md) 的源文档优先级处理：当前代码与迁移、当前 `docs/`、`AGENTS.md`、仍适用的 codegen 检查清单、历史 `plans/`。

需要按模块契约理解全项目依赖方向、数据对象所有权、配置影响范围和横向调试入口时，见 [全项目技术地图](full-project-technical-map.md)。

需要沿代码路径理解 L2/L3/L4、审批恢复、批量审批、email token、stale auto-approve 和 live executor preflight 时，见 [护栏与审批技术深挖](guardrail-approval-deep-dive.md)。

需要沿代码路径理解 fixture/live executor、action capability metadata、pre-action snapshot、execute preflight、verify gates、degraded rollback 和 API fixture 直执边界时，见 [执行器、动作能力与验证闭环技术深挖](executor-action-verification-loop-deep-dive.md)。

需要沿代码路径理解 `generate_report`、report regeneration、report version append-only、incident/run 状态同步、报告通知和前端报告页时，见 [报告生成、版本与事件生命周期技术深挖](report-generation-incident-lifecycle-deep-dive.md)。

需要沿代码路径理解工具调用、cache key、`tool_calls`、`evidence_items`、`evidence_id` 回填和 verify gates 时，见 [工具与证据技术深挖](tool-evidence-deep-dive.md)。

需要沿代码路径理解 runbook ingest/search、embedding 维度、memory scopes、context builder、压缩事件和 cache 指标边界时，见 [RAG、记忆与上下文技术深挖](rag-memory-context-deep-dive.md)。

需要沿代码路径理解 runbook draft 来源、publish 如何创建 `RunbookVersion`、draft chunk ingest 降级、M9 LLM draft pending-review、incident diff amendment review/apply 和不自动合并/发布的边界时，见 [Runbook 草稿、版本与 Amendment 生命周期技术深挖](runbook-draft-version-amendment-lifecycle-deep-dive.md)。

需要沿代码路径理解 `Settings`、M9 feature gates、discovery run/proposal、config publish、override TTL、EffectiveConfig 合并和 worker 只读 published config 时，见 [配置、Discovery 与 EffectiveConfig 技术深挖](config-discovery-effective-config-deep-dive.md)。

需要沿代码路径理解 DiscoveryRunner、K8s/Prometheus/Loki/Jaeger discovery、backend endpoint detection、capability matrix、workload binding、service edge、rerun lock 和 pending proposal 边界时，见 [Discovery、Capability Matrix 与服务拓扑技术深挖](discovery-capability-topology-deep-dive.md)。

需要沿代码路径理解 React 控制台、API client、TanStack Query、WebSocket ticket、Redis Pub/Sub 实时事件、审批弹窗和 service worker 通知时，见 [前端控制台与实时更新技术深挖](frontend-realtime-console-deep-dive.md)。

需要沿代码路径理解 FastAPI 控制面、router/service/repository 分层、认证 scope、错误信封、rate limit、事务提交点和写路径审计时，见 [API 控制面与服务层技术深挖](api-control-plane-service-deep-dive.md)。

需要沿代码路径理解 Celery worker、run 幂等、PostgresSaver、GraphInterrupt resume、node/tool audit、通知、poll/discovery/eval task 时，见 [Worker、Celery 与 LangGraph Checkpoint 技术深挖](worker-celery-langgraph-checkpoint-deep-dive.md)。

需要沿代码路径理解测试分层、CI coverage、FakeLLM smoke eval、EvalRun、shadow/replay 和 `/api/evals/engineering-metrics` 聚合时，见 [测试、Eval 与工程指标技术深挖](testing-eval-engineering-metrics-deep-dive.md)。

需要沿代码路径理解生产默认值、Compose/K8s profile、发布门禁、健康检查、M9 rollout、live backend 启用和回滚验证时，见 [生产发布、运维与回滚技术深挖](production-operations-rollback-deep-dive.md)。

需要沿代码路径理解 SQLAlchemy 模型、Alembic 迁移链、repository 事务边界、checkpoint pointer、pgvector fallback、audit append-only 和 API key 持久化边界时，见 [数据模型、迁移与持久化技术深挖](data-model-migrations-persistence-deep-dive.md)。

需要沿代码路径理解 API key middleware、scope、bootstrap seed、WebSocket ticket、rate limit、audit log、secret redaction 和生产认证检查时，见 [认证、API Key、审计与安全边界技术深挖](auth-api-key-audit-security-deep-dive.md)。

需要沿代码路径理解 email queue/log、approval email token、incident comments、evidence annotations、WebSocket/service worker 通知和操作员审计串联时，见 [通知、邮件、评论协作与操作员交互技术深挖](notifications-collaboration-operator-interaction-deep-dive.md)。

需要沿代码路径理解 NFA 标记、根因/action 反馈、相关事件、runbook feedback analyzer、amendment draft 和 memory/eval 回流边界时，见 [反馈、NFA、关联事件与持续学习技术深挖](feedback-nfa-correlation-continuous-learning-deep-dive.md)。

需要沿代码路径理解 Prometheus、Loki、Trace、Deployment、K8s 和 DB 诊断后端如何通过 worker deps、EffectiveConfig、URL safety、request-local cache 和 read-only live backend 接入时，见 [Observability 与后端适配器技术深挖](observability-backend-adapters-deep-dive.md)。

需要沿代码路径理解 deployment change、GitHub deployments/commits fallback、Argo CD sync history、deployment evidence 与 rollback action 边界时，见 [Deployment Change、GitHub、Argo CD 与发布变更证据技术深挖](deployment-change-github-argocd-deep-dive.md)。

需要沿代码路径理解 LLM provider 工厂、FakeLLM 确定性覆盖、prompt/JSON fallback、usage metadata、reasoning redaction、真实 provider 手动边界和 M9 draft-only 能力时，见 [LLM、Prompt、FakeLLM 与 Provider 边界技术深挖](llm-prompt-fakellm-provider-boundaries-deep-dive.md)。

## 常见定位路径

| 任务 | 先看代码 | 再看文档 |
|------|----------|----------|
| 理解全项目模块契约 | `apps/`、`packages/`、`tests/`、`deploy/` | `docs/00-overview/full-project-technical-map.md` |
| 新增 API | `apps/api/routers`、`schemas`、`services`、`packages/db/repositories` | `docs/01-backend/api-reference.md`、`docs/00-overview/api-control-plane-service-deep-dive.md` |
| 修改认证、API key、scope 或审计 | `apps/api/middleware/auth.py`、`apps/api/dependencies.py`、`apps/api/routers/api_keys.py`、`apps/api/services/ws_ticket_service.py`、`packages/db/repositories/audit_logs.py`、`packages/common/redaction.py` | `docs/01-backend/auth-and-api-keys.md`、`docs/00-overview/auth-api-key-audit-security-deep-dive.md` |
| 修改通知、邮件、评论或操作员交互 | `apps/api/services/email_service.py`、`apps/worker/tasks.py`、`apps/api/routers/approvals.py`、`apps/api/routers/comments.py`、`apps/api/ws`、`apps/web/src/App.tsx`、`apps/web/public/sw.js` | `docs/01-backend/celery-and-jobs.md`、`docs/06-frontend/react-console.md`、`docs/00-overview/notifications-collaboration-operator-interaction-deep-dive.md` |
| 修改 NFA、feedback、相关事件或持续学习边界 | `apps/api/services/feedback_service.py`、`apps/api/routers/incidents.py`、`packages/db/repositories/false_positive_patterns.py`、`packages/db/repositories/feedback.py`、`packages/db/repositories/incident_correlations.py`、`packages/discovery/runbook_feedback.py`、`apps/api/services/runbook_service.py` | `docs/00-overview/feedback-nfa-correlation-continuous-learning-deep-dive.md`、`docs/01-backend/api-reference.md`、`docs/04-rag/runbook-rag.md`、`docs/05-memory/memory-cache-compression.md` |
| 修改数据模型或迁移 | `packages/db/models.py`、`packages/db/session.py`、`packages/db/repositories`、`migrations/versions` | `docs/01-backend/data-model.md`、`docs/00-overview/data-model-migrations-persistence-deep-dive.md` |
| 修改 Worker/Celery 任务 | `apps/worker/tasks.py`、`apps/worker/celery_app.py`、`packages/agent/runner.py` | `docs/01-backend/celery-and-jobs.md`、`docs/00-overview/worker-celery-langgraph-checkpoint-deep-dive.md` |
| 修改 Agent 节点 | `packages/agent/graph.py`、`packages/agent/nodes`、`packages/agent/state.py` | `docs/02-agent/workflow.md` |
| 修改报告生成、再生成或事件生命周期 | `packages/agent/nodes/generate_report.py`、`apps/api/services/report_service.py`、`apps/worker/tasks.py`、`apps/web/src/App.tsx` | `docs/00-overview/report-generation-incident-lifecycle-deep-dive.md`、`docs/01-backend/api-reference.md`、`docs/06-frontend/react-console.md` |
| 修改 LLM provider、prompt、FakeLLM 或真实 provider 边界 | `packages/agent/llm`、`packages/agent/prompts.py`、`packages/agent/fake_llm.py`、`packages/agent/rules_fallback.py`、`packages/evals` | `docs/02-agent/llm-and-prompts.md`、`docs/00-overview/llm-prompt-fakellm-provider-boundaries-deep-dive.md`、`docs/09-evals/evaluation.md` |
| 调整风险规则 | `packages/agent/guardrails/policy.py`、approval service/node | `docs/02-agent/guardrails-and-approval.md`、`docs/00-overview/guardrail-approval-deep-dive.md` |
| 修改执行动作、executor 或验证闭环 | `packages/agent/actions/capabilities.py`、`packages/agent/nodes/take_snapshot.py`、`packages/agent/nodes/execute_action.py`、`packages/agent/nodes/verify.py`、`packages/tools/executor_backends.py` | `docs/00-overview/executor-action-verification-loop-deep-dive.md`、`docs/02-agent/guardrails-and-approval.md`、`docs/03-tools/tool-layer.md` |
| 新增工具后端或 observability backend | `packages/tools`、`apps/worker/tasks.py`、`packages/discovery` | `docs/03-tools/tool-layer.md`、`docs/00-overview/tool-evidence-deep-dive.md`、`docs/00-overview/observability-backend-adapters-deep-dive.md` |
| 修改 deployment change、GitHub 或 Argo CD 变更证据 | `packages/tools/git_changes.py`、`packages/tools/deployment_backends.py`、`packages/agent/nodes/collect_deployment.py`、`apps/worker/tasks.py` | `docs/00-overview/deployment-change-github-argocd-deep-dive.md`、`docs/03-tools/tool-layer.md`、`docs/11-reference/configuration.md` |
| 修改 runbook ingest/search、draft、version 或 amendment | `packages/rag`、`apps/api/services/runbook_service.py`、runbook repositories | `docs/04-rag/runbook-rag.md`、`docs/00-overview/rag-memory-context-deep-dive.md`、`docs/00-overview/runbook-draft-version-amendment-lifecycle-deep-dive.md` |
| 修改上下文压缩 | `packages/memory`、`packages/agent/nodes/build_context.py` | `docs/05-memory/memory-cache-compression.md`、`docs/00-overview/rag-memory-context-deep-dive.md` |
| 修改配置发布、override 或 EffectiveConfig | `packages/common/settings.py`、`feature_flags.py`、`packages/discovery/config_*`、`apps/worker/tasks.py` | `docs/11-reference/configuration.md`、`docs/00-overview/config-discovery-effective-config-deep-dive.md` |
| 修改 discovery、能力矩阵或服务拓扑 | `packages/discovery`、`apps/api/routers/discovery.py`、`apps/worker/tasks.py` | `docs/00-overview/discovery-capability-topology-deep-dive.md`、`docs/01-backend/api-reference.md`、`docs/08-deploy/k8s-backend-verification.md` |
| 修改本地部署 | `docker-compose.yml`、`deploy/`、`demo/` | `docs/08-deploy/local-demo.md` |
| 验证 K8s 后端对接 | `deploy/k8s/`、`apps/worker/tasks.py`、`packages/discovery` | `docs/08-deploy/k8s-backend-verification.md`、`docs/11-reference/backend-connectivity.md`、`docs/00-overview/observability-backend-adapters-deep-dive.md` |
| 准备生产发布或回滚 | `packages/common/settings.py`、`deploy/k8s/`、`docker-compose.yml`、`apps/api/routers/health.py` | `docs/production-checklist.md`、`docs/final-pre-execution-checklist.md`、`docs/00-overview/production-operations-rollback-deep-dive.md` |
| 修改前端 | `apps/web/src`、`apps/api/ws` | `docs/06-frontend/react-console.md`、`docs/00-overview/frontend-realtime-console-deep-dive.md` |
| 修改测试、Eval 或工程指标 | `tests/`、`packages/evals`、`apps/api/services/engineering_metrics_service.py` | `docs/07-testing/testing-strategy.md`、`docs/09-evals/evaluation.md`、`docs/00-overview/testing-eval-engineering-metrics-deep-dive.md` |
