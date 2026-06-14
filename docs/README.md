# SRE 事件响应 Agent — 文档中心

**最后更新：** 2026-06-14

本文档中心面向开发者、测试人员、运维人员和贡献者。它描述的是系统已构建的现状（而非计划中的状态）。设计历史请参见 `plans/`。

## 快速链接

| 需求 | 前往 |
|------|-------|
| 新开发者从零了解项目 | [开发者全景指南](00-overview/developer-guide.md) |
| 查看文档分批更新计划 | [文档更新批次计划](00-overview/documentation-update-plan.md) |
| 本地快速开始 | [快速开始](00-overview/quick-start.md) |
| 了解系统架构 | [架构](00-overview/architecture.md) |
| 运行完整技术栈 | [本地演示](08-deploy/local-demo.md) |
| 查找配置项 | [配置参考](11-reference/configuration.md) |
| 了解 Agent 工作流 | [Agent 工作流](02-agent/workflow.md) |
| 查找 API 端点 | [API 参考](01-backend/api-reference.md) |
| 阅读 M9 增强功能 | [M9 发布计划](m9-rollout.md) |
| 准备生产环境 | [生产环境检查清单](production-checklist.md) |
| 执行运维操作手册 | [运维 Runbook](operator-runbook.md) |

## 文档章节

### `00-overview` — 项目概览
- [项目概览](00-overview/project-overview.md)
- [开发者全景指南](00-overview/developer-guide.md)
- [文档更新批次计划](00-overview/documentation-update-plan.md)
- [架构](00-overview/architecture.md)
- [范围与边界](00-overview/scope-and-boundaries.md)
- [快速开始](00-overview/quick-start.md)
- [仓库地图](00-overview/repository-map.md)

### `01-backend` — 后端 / API
- [API 参考](01-backend/api-reference.md) — 76 条业务 HTTP route + 1 个 WebSocket
- [数据模型](01-backend/data-model.md) — 32 个 SQLAlchemy 模型
- [后端架构](01-backend/backend-architecture.md)
- [认证与 API 密钥](01-backend/auth-and-api-keys.md)
- [Celery 与任务](01-backend/celery-and-jobs.md)
- [错误与请求 ID](01-backend/errors-and-request-ids.md)

### `02-agent` — Agent 工作流
- [工作流](02-agent/workflow.md) — 18 节点 LangGraph 图，含 ReAct 循环
- [护栏与审批](02-agent/guardrails-and-approval.md)
- [LLM 与提示词](02-agent/llm-and-prompts.md)

### `03-tools` — 工具层
- [工具层](03-tools/tool-layer.md) — 15 个工具模块

### `04-rag` — Runbook RAG
- [Runbook RAG](04-rag/runbook-rag.md) — 20 个 RAG 模块

### `05-memory` — 记忆与上下文
- [记忆、缓存与压缩](05-memory/memory-cache-compression.md)

### `06-frontend` — React 控制台
- [React 控制台](06-frontend/react-console.md)

### `07-testing` — 测试策略
- [测试策略](07-testing/testing-strategy.md)

### `08-deploy` — 本地部署
- [本地演示](08-deploy/local-demo.md)

### `09-evals` — 评估
- [评估](09-evals/evaluation.md)

### `10-operations` — 运维
- [Runbook](10-operations/runbook.md)
- [开发工作流](10-operations/development-workflow.md)
- [演示操作手册](10-operations/demo-playbook.md)

### `11-reference` — 参考
- [配置参考](11-reference/configuration.md) — 100+ 配置项
- [术语表](11-reference/glossary.md)
- [状态与 ID](11-reference/status-and-ids.md)

### `superpowers/specs/` — 设计与实现规格
- [真实后端集成设计（M0–M8）](superpowers/specs/2026-06-10-real-backend-integration-design.md)
- [真实后端集成计划（M0–M8）](superpowers/specs/2026-06-11-real-backend-integration-implementation-plan.md)
- [M9 Agent 执行计划](superpowers/specs/m9-foragent.md)

## 顶层文档

| 文档 | 用途 |
|----------|---------|
| [m9-rollout.md](m9-rollout.md) | M9 发布策略、特性开关、回滚 |
| [m9-data-flow.md](m9-data-flow.md) | M9 数据流图 |
| [m9-threat-model.md](m9-threat-model.md) | M9 安全威胁模型 |
| [operator-runbook.md](operator-runbook.md) | Day-2 运维 |
| [production-checklist.md](production-checklist.md) | 生产环境上线前检查清单 |
| [final-pre-execution-checklist.md](final-pre-execution-checklist.md) | 发布门禁验证 |

## 当前状态

**M0–M8 已完成（41 个 PR）：** 真实后端集成、确定性诊断、安全发布、配置合并、审计、回滚、runbook 审查、Alertmanager 轮询、服务发现、证据验证、级联故障分析、K8s 执行器、评估、Agent 编排、ReAct 验证循环。

**M9 受控增强：** AI LLM 生成/差异分析、Web 搜索安全、Tempo 追踪后端、Grafana Webhook 接收、语义化 runbook 搜索、外部嵌入提供商均按当前代码形态记录在文档中，全部在显式特性开关之后（生产环境默认关闭）。

## 关键边界

- API 绝不在请求中内联运行诊断；`POST /api/alerts` 仅持久化并入队到 Celery。
- L2/L3 操作需要人工审批。L3 需要 `risk_ack=true`、`confirm_action_type`、`confirm_target`。
- L4 操作直接拒绝，绝不进入审批流程。
- CI、单元测试和 smoke eval 使用 FakeLLM。
- 默认执行器为 fixture；live 执行器需显式选择加入。
- M9 增强功能在生产环境中全部默认关闭，受 `M9_EXTENSIONS_ENABLED` 控制。

## 与 `plans/` 的关系

`plans/` 保留了原始的实施方案（编写于 2026-06-07）。`docs/` 是当前面向读者的文档，描述已完成的各项能力。当行为发生变更时，请更新对应的 `docs/` 文件。
