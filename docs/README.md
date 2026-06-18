# SRE 事件响应 Agent — 文档中心

**最后更新：** 2026-06-18

本文档中心面向开发者、测试人员、运维人员和贡献者。它描述的是系统已构建的现状（而非计划中的状态）。设计历史请参见 `plans/`。

## 快速链接

| 需求 | 前往 |
|------|-------|
| 新开发者从零了解项目 | [开发者全景指南](00-overview/developer-guide.md) |
| 查看文档分批更新计划 | [文档更新批次计划](00-overview/documentation-update-plan.md) |
| 查看工程评估指标 | [工程评估指标](00-overview/engineering-metrics.md) |
| 深入理解告警到报告链路 | [告警到报告技术深挖](00-overview/alert-to-report-deep-dive.md) |
| 深入理解告警来源归一化与 Alertmanager Poll | [Alertmanager Poll、Grafana 与告警来源归一化技术深挖](00-overview/alert-source-normalization-poll-grafana-deep-dive.md) |
| 深入理解护栏与审批链路 | [护栏与审批技术深挖](00-overview/guardrail-approval-deep-dive.md) |
| 深入理解执行器、动作能力与验证闭环 | [执行器、动作能力与验证闭环技术深挖](00-overview/executor-action-verification-loop-deep-dive.md) |
| 深入理解报告生成、版本与事件生命周期 | [报告生成、版本与事件生命周期技术深挖](00-overview/report-generation-incident-lifecycle-deep-dive.md) |
| 深入理解 Runbook 草稿、版本与 Amendment 生命周期 | [Runbook 草稿、版本与 Amendment 生命周期技术深挖](00-overview/runbook-draft-version-amendment-lifecycle-deep-dive.md) |
| 深入理解工具与证据链路 | [工具与证据技术深挖](00-overview/tool-evidence-deep-dive.md) |
| 深入理解 RAG/记忆/上下文链路 | [RAG、记忆与上下文技术深挖](00-overview/rag-memory-context-deep-dive.md) |
| 深入理解配置与 discovery 链路 | [配置、Discovery 与 EffectiveConfig 技术深挖](00-overview/config-discovery-effective-config-deep-dive.md) |
| 深入理解 Discovery、能力矩阵与服务拓扑 | [Discovery、Capability Matrix 与服务拓扑技术深挖](00-overview/discovery-capability-topology-deep-dive.md) |
| 深入理解 Observability 后端适配器 | [Observability 与后端适配器技术深挖](00-overview/observability-backend-adapters-deep-dive.md) |
| 深入理解 Deployment Change、GitHub 与 Argo CD 变更证据 | [Deployment Change、GitHub、Argo CD 与发布变更证据技术深挖](00-overview/deployment-change-github-argocd-deep-dive.md) |
| 深入理解 LLM、Prompt 与 FakeLLM 边界 | [LLM、Prompt、FakeLLM 与 Provider 边界技术深挖](00-overview/llm-prompt-fakellm-provider-boundaries-deep-dive.md) |
| 深入理解前端实时控制台链路 | [前端控制台与实时更新技术深挖](00-overview/frontend-realtime-console-deep-dive.md) |
| 深入理解 API 控制面与服务层 | [API 控制面与服务层技术深挖](00-overview/api-control-plane-service-deep-dive.md) |
| 深入理解 Worker 执行面与 checkpoint | [Worker、Celery 与 LangGraph Checkpoint 技术深挖](00-overview/worker-celery-langgraph-checkpoint-deep-dive.md) |
| 深入理解测试、Eval 与工程指标 | [测试、Eval 与工程指标技术深挖](00-overview/testing-eval-engineering-metrics-deep-dive.md) |
| 深入理解生产发布、运维与回滚 | [生产发布、运维与回滚技术深挖](00-overview/production-operations-rollback-deep-dive.md) |
| 深入理解数据模型、迁移与持久化 | [数据模型、迁移与持久化技术深挖](00-overview/data-model-migrations-persistence-deep-dive.md) |
| 深入理解认证、API Key 与审计安全 | [认证、API Key、审计与安全边界技术深挖](00-overview/auth-api-key-audit-security-deep-dive.md) |
| 深入理解通知、邮件与操作员协作 | [通知、邮件、评论协作与操作员交互技术深挖](00-overview/notifications-collaboration-operator-interaction-deep-dive.md) |
| 深入理解反馈、NFA 与持续学习边界 | [反馈、NFA、关联事件与持续学习技术深挖](00-overview/feedback-nfa-correlation-continuous-learning-deep-dive.md) |
| 查看全项目模块契约 | [全项目技术地图](00-overview/full-project-technical-map.md) |
| 本地快速开始 | [快速开始](00-overview/quick-start.md) |
| 了解系统架构 | [架构](00-overview/architecture.md) |
| 运行完整技术栈 | [本地演示](08-deploy/local-demo.md) |
| 验证 K8s 后端对接 | [K8s 后端对接验证](08-deploy/k8s-backend-verification.md) |
| 理解单后端/多服务边界 | [后端对接范围](11-reference/backend-connectivity.md) |
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
- [工程评估指标](00-overview/engineering-metrics.md)
- [告警到报告技术深挖](00-overview/alert-to-report-deep-dive.md)
- [Alertmanager Poll、Grafana 与告警来源归一化技术深挖](00-overview/alert-source-normalization-poll-grafana-deep-dive.md)
- [护栏与审批技术深挖](00-overview/guardrail-approval-deep-dive.md)
- [执行器、动作能力与验证闭环技术深挖](00-overview/executor-action-verification-loop-deep-dive.md)
- [报告生成、版本与事件生命周期技术深挖](00-overview/report-generation-incident-lifecycle-deep-dive.md)
- [Runbook 草稿、版本与 Amendment 生命周期技术深挖](00-overview/runbook-draft-version-amendment-lifecycle-deep-dive.md)
- [工具与证据技术深挖](00-overview/tool-evidence-deep-dive.md)
- [RAG、记忆与上下文技术深挖](00-overview/rag-memory-context-deep-dive.md)
- [配置、Discovery 与 EffectiveConfig 技术深挖](00-overview/config-discovery-effective-config-deep-dive.md)
- [Discovery、Capability Matrix 与服务拓扑技术深挖](00-overview/discovery-capability-topology-deep-dive.md)
- [Observability 与后端适配器技术深挖](00-overview/observability-backend-adapters-deep-dive.md)
- [Deployment Change、GitHub、Argo CD 与发布变更证据技术深挖](00-overview/deployment-change-github-argocd-deep-dive.md)
- [LLM、Prompt、FakeLLM 与 Provider 边界技术深挖](00-overview/llm-prompt-fakellm-provider-boundaries-deep-dive.md)
- [前端控制台与实时更新技术深挖](00-overview/frontend-realtime-console-deep-dive.md)
- [API 控制面与服务层技术深挖](00-overview/api-control-plane-service-deep-dive.md)
- [Worker、Celery 与 LangGraph Checkpoint 技术深挖](00-overview/worker-celery-langgraph-checkpoint-deep-dive.md)
- [测试、Eval 与工程指标技术深挖](00-overview/testing-eval-engineering-metrics-deep-dive.md)
- [生产发布、运维与回滚技术深挖](00-overview/production-operations-rollback-deep-dive.md)
- [数据模型、迁移与持久化技术深挖](00-overview/data-model-migrations-persistence-deep-dive.md)
- [认证、API Key、审计与安全边界技术深挖](00-overview/auth-api-key-audit-security-deep-dive.md)
- [通知、邮件、评论协作与操作员交互技术深挖](00-overview/notifications-collaboration-operator-interaction-deep-dive.md)
- [反馈、NFA、关联事件与持续学习技术深挖](00-overview/feedback-nfa-correlation-continuous-learning-deep-dive.md)
- [全项目技术地图](00-overview/full-project-technical-map.md)
- [架构](00-overview/architecture.md)
- [范围与边界](00-overview/scope-and-boundaries.md)
- [快速开始](00-overview/quick-start.md)
- [仓库地图](00-overview/repository-map.md)

### `01-backend` — 后端 / API
- [API 参考](01-backend/api-reference.md) — 79 条业务 HTTP route + 1 个 WebSocket
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

### `08-deploy` — 部署
- [本地演示](08-deploy/local-demo.md)
- [K8s 后端对接验证](08-deploy/k8s-backend-verification.md)

### `09-evals` — 评估
- [评估](09-evals/evaluation.md)

### `10-operations` — 运维
- [Runbook](10-operations/runbook.md)
- [开发工作流](10-operations/development-workflow.md)
- [演示操作手册](10-operations/demo-playbook.md)

### `11-reference` — 参考
- [配置参考](11-reference/configuration.md) — 100+ 配置项
- [后端对接范围](11-reference/backend-connectivity.md)
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

**M9 受控增强：** AI LLM 生成/差异分析、Web 搜索安全、Tempo 追踪后端、Grafana-shaped 告警归一化与未暴露 helper、语义化 runbook 搜索、外部嵌入提供商均按当前代码形态记录在文档中，全部在显式特性开关之后（生产环境默认关闭）。

## 关键边界

- API 绝不在请求中内联运行诊断；`POST /api/alerts` 仅持久化并入队到 Celery。
- L2/L3 操作需要人工审批。L3 需要 `risk_ack=true`、`confirm_action_type`、`confirm_target`。
- L4 操作直接拒绝，绝不进入审批流程。
- CI、单元测试和 smoke eval 使用 FakeLLM。
- 默认执行器为 fixture；live 执行器需显式选择加入。
- M9 增强功能在生产环境中全部默认关闭，受 `M9_EXTENSIONS_ENABLED` 控制。

## 与 `plans/` 的关系

`plans/` 保留了原始的实施方案（编写于 2026-06-07）。`docs/` 是当前面向读者的文档，描述已完成的各项能力。当行为发生变更时，请更新对应的 `docs/` 文件。
