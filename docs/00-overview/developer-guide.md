# 开发者全景指南

**最后更新：** 2026-06-13

本文面向首次接手项目的开发者。目标是在不替代各专题文档的前提下，给出一条可执行的阅读路径和改动入口，帮助开发者理解系统的整体形状、边界、代码位置和验证方式。

## 阅读顺序

1. 先读 [范围与安全边界](scope-and-boundaries.md)，明确默认安全路径、live executor 限制、审批边界和 M9 默认关闭原则。
2. 再读 [快速开始](quick-start.md) 和 [本地演示](../08-deploy/local-demo.md)，把本地数据库、Redis、API、worker、web 控制台跑起来。
3. 用 [仓库地图](repository-map.md) 对照代码目录，确认 `apps/`、`packages/`、`tests/`、`migrations/`、`demo/`、`deploy/` 的职责。
4. 读 [后端架构](../01-backend/backend-architecture.md)、[API 参考](../01-backend/api-reference.md)、[数据模型](../01-backend/data-model.md)，理解请求、事务、repository 和持久化模型。
5. 读 [Agent 工作流](../02-agent/workflow.md)、[护栏与审批](../02-agent/guardrails-and-approval.md)、[LLM 与提示词](../02-agent/llm-and-prompts.md)，理解 LangGraph 节点、checkpoint、FakeLLM 和人机回环。
6. 读 [工具层](../03-tools/tool-layer.md)、[Runbook RAG](../04-rag/runbook-rag.md)、[记忆、缓存与压缩](../05-memory/memory-cache-compression.md)，理解证据、检索、缓存和上下文预算。
7. 读 [React 控制台](../06-frontend/react-console.md)，理解页面、轮询、审批和 Agent run 可视化。
8. 读 [测试策略](../07-testing/testing-strategy.md)、[评估](../09-evals/evaluation.md)、[开发工作流](../10-operations/development-workflow.md)，确认每类变更需要的测试层级。
9. 最后读 [配置参考](../11-reference/configuration.md)、[状态与 ID](../11-reference/status-and-ids.md)、[术语表](../11-reference/glossary.md)，用于查细节。

## 源文档优先级

当文档之间出现差异时，按以下顺序判断：

1. 当前代码与迁移。
2. `docs/` 下对应模块文档。
3. `AGENTS.md` 中的安全边界和编码规则。
4. `plans/10-codegen/` 中仍适用的生成检查清单。
5. `plans/` 和 `plans/11-roadmap/` 的历史规划背景。

`plans/` 保留历史设计，不应覆盖当前 `docs/` 或代码。例如当前 schema 使用 512 维 embedding，旧计划中出现的 384 维口径不能作为实现依据。

## 系统心智模型

```text
告警输入
  -> FastAPI 标准化、去重、创建 Incident + AgentRun
  -> Celery 异步执行诊断任务
  -> LangGraph 通过工具层收集 metrics/logs/traces/deployment/K8s/DB/runbook/memory 证据
  -> 诊断、压缩、排序、规划动作
  -> 确定性 guardrail 判定 L0-L4 风险
  -> L0/L1 自动执行，L2/L3 等待审批，L4 直接拒绝
  -> 执行前快照、fixture 或显式 live K8s executor、验证、必要时重新规划
  -> 生成报告、持久化记忆、前端展示
```

核心约束：

- API 请求中不内联运行 LangGraph；诊断必须通过 Celery。
- Worker 使用 LangGraph `PostgresSaver` checkpoint；`agent_runs.state` 只做展示快照。
- 默认使用 FakeLLM、fixture 工具和 fixture executor；CI 与 smoke eval 不依赖真实 LLM。
- Live K8s executor 必须显式启用，且只允许已记录的 restart/pause/resume/scale/rollback Kubernetes mutation。
- L2/L3 操作必须审批；L3 必须带二次确认字段；L4 永远不进入审批。
- 原始密钥不落库、不进日志、不进审计、不进提示词。
- 大日志和超预算证据必须先压缩，诊断输出要保留 evidence ID 或 runbook chunk ID。
- M9 能力是增强，不替代 M0-M8 确定性路径；生产环境默认关闭。

## 改动入口

| 变更类型 | 主要代码位置 | 必改文档 | 常见测试 |
|----------|--------------|----------|----------|
| 新增或修改 API | `apps/api/routers/`、`apps/api/schemas/`、`apps/api/services/`、`packages/db/repositories/` | `docs/01-backend/api-reference.md`、`docs/01-backend/backend-architecture.md` | `tests/unit/`、`tests/integration/`、契约测试 |
| 修改数据库模型 | `packages/db/models.py`、`migrations/versions/`、repository | `docs/01-backend/data-model.md`、`docs/11-reference/status-and-ids.md` | repository 单测、迁移相关集成测试 |
| 新增 Agent 节点或路由 | `packages/agent/graph.py`、`packages/agent/nodes/`、`packages/agent/state.py` | `docs/02-agent/workflow.md` | 节点单测、checkpoint/resume 集成测试 |
| 修改 guardrail 或审批 | `packages/agent/guardrails/`、`packages/agent/nodes/human_approval.py`、approval service | `docs/02-agent/guardrails-and-approval.md` | L2/L3/L4 风险测试、审批冲突测试 |
| 新增工具或后端 | `packages/tools/` | `docs/03-tools/tool-layer.md`、必要时 `docs/11-reference/configuration.md` | mocked backend 单测、降级路径测试 |
| 修改 RAG | `packages/rag/`、runbook repository | `docs/04-rag/runbook-rag.md` | embedding determinism、search result contract |
| 修改记忆或压缩 | `packages/memory/`、agent context 节点 | `docs/05-memory/memory-cache-compression.md` | 压缩触发、evidence ID 保留、cache 指标拆分 |
| 修改前端页面 | `apps/web/src/` | `docs/06-frontend/react-console.md` | Vitest、React Testing Library、Playwright |
| 新增配置或 feature flag | `packages/common/settings.py`、`packages/common/feature_flags.py` | `docs/11-reference/configuration.md`、相关模块文档 | settings 单测、生产默认值测试 |
| 修改部署或 demo | `docker-compose.yml`、`deploy/`、`demo/` | `docs/08-deploy/local-demo.md`、`docs/10-operations/demo-playbook.md` | compose smoke、E2E |
| 修改 M9 能力 | 对应模块 + feature gate | `docs/m9-rollout.md`、`docs/m9-data-flow.md`、`docs/m9-threat-model.md` | 默认关闭、回滚、脱敏、审计/指标测试 |

## 本地验证命令

```bash
pytest tests/unit tests/integration --cov=apps --cov=packages --cov-report=term-missing --cov-report=xml --cov-fail-under=80
ruff check apps packages tests
mypy apps packages
```

前端命令在 `apps/web/` 下运行：

```bash
npm run test:coverage
npm run test:e2e
npm run build
```

文档类改动至少需要人工检查新增链接和相关专题文档是否同步。若文档描述了可运行命令、API 请求或配置默认值，应对照代码或测试确认。

## 文档维护规则

- 文档描述当前行为，不把未实现计划写成已完成能力。
- 行为变更要更新对应模块文档，不只更新 README 或索引。
- 旧计划文档可以引用为背景，但不得作为放宽安全边界的依据。
- 新增安全敏感能力时，必须写明默认值、feature flag、失败降级、审计/指标、回滚方式和测试覆盖。
- 新增真实外部调用时，必须写明超时、脱敏、错误降级、密钥处理和测试策略。
- 文档中的数量统计应从代码或测试输出校准；不确定时使用范围或描述性语言，避免写死容易过期的数字。
