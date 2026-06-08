# SRE Incident Response Agent 文档中心

本文档中心面向开发、测试、演示和后续维护。内容依据当前仓库实现、`plans/` 下的设计文档和项目约束整理。当前项目按本地 demo 与已纳入仓库的扩展范围视为完成；`plans/11-roadmap/` 保留阶段来源和完成记录，但不代表默认放宽生产安全边界。

## 推荐阅读顺序

1. [项目概览](00-overview/project-overview.md)
2. [系统架构](00-overview/architecture.md)
3. [范围与安全边界](00-overview/scope-and-boundaries.md)
4. [快速开始](00-overview/quick-start.md)
5. [仓库地图](00-overview/repository-map.md)
6. [后端 API 总览](01-backend/api-reference.md)
7. [数据模型](01-backend/data-model.md)
8. [Agent 工作流](02-agent/workflow.md)
9. [Guardrail 与审批](02-agent/guardrails-and-approval.md)
10. [工具层](03-tools/tool-layer.md)
11. [Runbook RAG](04-rag/runbook-rag.md)
12. [记忆、缓存与压缩](05-memory/memory-cache-compression.md)
13. [前端控制台](06-frontend/react-console.md)
14. [测试策略](07-testing/testing-strategy.md)
15. [本地部署与演示](08-deploy/local-demo.md)
16. [评测体系](09-evals/evaluation.md)
17. [运维手册](10-operations/runbook.md)
18. [开发与验收流程](10-operations/development-workflow.md)
19. [配置参考](11-reference/configuration.md)

如果你是第一次读这个项目，建议同时按根目录的 [学习计划](../study.md) 逐周练习。

## 文档分区

| 分区 | 内容 |
| --- | --- |
| `00-overview` | 系统目标、架构、范围、快速开始、仓库结构 |
| `01-backend` | FastAPI、Pydantic schema、服务层、仓储层、Celery、错误模型 |
| `02-agent` | LangGraph 节点、状态、依赖注入、审批恢复、报告生成 |
| `03-tools` | Prometheus、Loki、Trace、Git、K8s、DB 诊断、mock executor |
| `04-rag` | Runbook 入库、切分、embedding（Fake/BGE-ZH/text2vec）、混合检索、rerank、草稿和版本 |
| `05-memory` | 多级记忆、token 预算、上下文压缩、缓存指标 |
| `06-frontend` | React + TypeScript + Vite 控制台页面、状态处理、E2E |
| `07-testing` | 单元、集成、契约、E2E、覆盖率、手动邮件测试 |
| `08-deploy` | Docker Compose、本地端口、BGE-ZH/Mailpit、服务依赖、迁移、演示数据 |
| `09-evals` | FakeLLM smoke eval、full eval、shadow eval、指标 |
| `10-operations` | 常见操作、故障排查、审批处理、演示流程、开发验收 |
| `11-reference` | 配置项、ID 前缀、状态枚举、API 错误、术语表 |

## 当前完成状态

项目已完成文档范围内的本地 demo SRE 事故响应 Agent。API 接收告警后创建 incident 和 agent run，通过 Celery 异步运行 LangGraph 诊断工作流，采集指标、日志、trace、部署变更、Kubernetes 事件、数据库诊断、历史记忆和 Runbook 证据，生成根因分析、推荐动作、审批请求和事故报告。

已纳入仓库的扩展能力包括 LLM provider adapter（Fake/OpenAI/DeepSeek/Anthropic/vLLM）、LLM reasoning 节点、证据交叉验证与级联故障分析、可配置只读工具后端、BGE-ZH embedding 支持、邮件通知、Runbook 混合检索与版本、反馈/跨事故关联、协作审批、API key、Prometheus metrics、WebSocket 节点事件、Celery beat 周期任务、eval 与 shadow eval。

默认配置使用 FakeLLM、fixture 数据源、FakeEmbedding 和 mock executor。即使代码中存在可选的真实后端配置，MVP 安全边界仍然是：不执行真实生产 Kubernetes 写操作，不执行真实云资源写操作，不删除数据，不修改真实数据库，不 flush 真实缓存。

## 关键边界

- API 不在线内执行诊断；`POST /api/alerts` 只落库并入队 Celery。
- L2/L3 动作必须走人工审批。
- L3 审批必须提供 `risk_ack=true`、`confirm_action_type` 和 `confirm_target`。
- L4 动作直接拒绝，不进入审批，不允许执行。
- CI、单元测试和 smoke eval 使用 FakeLLM。
- 所有执行动作在 MVP 中使用 mock executor。

## 与 `plans/` 的关系

`plans/` 保留实现级规划、roadmap 来源和阶段完成记录。`docs/` 是面向读者的当前项目文档，描述已完成能力、运行步骤和维护流程。若后续实现发生变化，应同步更新对应 `docs/` 文档；若只是规划或复盘信息变化，应优先更新 `plans/`。
