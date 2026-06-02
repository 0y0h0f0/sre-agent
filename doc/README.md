# SRE Incident Response Agent 文档索引

本文档集用于指导代码生成和后续实现。`plan.md` 是高层计划，`doc/` 是实现级来源；当二者出现细节差异时，以 `doc/` 和 `agent.md` 的更具体约束为准。固定技术栈为 FastAPI、LangGraph、Celery、PostgreSQL、pgvector、Redis、Prometheus、Loki、OpenTelemetry、React + TypeScript + Vite。

## 阅读顺序

1. `00-overview/architecture.md`：系统总览、模块边界、主链路。
2. `00-overview/scope.md`：MVP 范围、非目标、风险边界。
3. `00-overview/engineering-metrics.md`：功能、性能、可靠性和质量指标。
4. `01-backend/`：FastAPI、数据模型、Celery 和配置约定。
5. `02-agent/`：LangGraph 状态机、节点、审批和 guardrail。
6. `03-tools/`：Prometheus、Loki、Trace、Git、Action 工具实现。
7. `04-rag/`：Runbook RAG 入库、检索、rerank 和引用。
8. `05-memory/`：token 缓存、多级记忆、上下文压缩策略。
9. `06-frontend/`：React 控制台页面与组件结构。
10. `07-testing/`：单元、集成、契约、E2E、覆盖率门禁。
11. `08-deploy/`：Docker Compose demo 环境。
12. `09-evals/`：故障评测集与指标计算。
13. `10-codegen/`：代码生成顺序、模块清单、验收检查。
14. `10-codegen/documentation-quality-gate.md`：代码生成前的文档质量门禁。
15. `11-roadmap/`：MVP 之后的拓展计划（Phase 1-8），来源于 `tzplan.md`。

## 代码生成原则

- 先生成稳定边界：schema、数据库模型、工具接口、API contract。
- 再生成业务流程：Celery task、LangGraph 节点、审批恢复。
- 最后生成 UI、E2E、评测和文档包装。
- 测试优先于复杂优化。每个模块生成后必须有最小可运行测试。
- 默认使用 FakeLLM、mock Prometheus、mock Loki、mock executor，避免测试依赖真实外部系统。

## 目录目标

```text
doc/
  00-overview/
  01-backend/
  02-agent/
  03-tools/
  04-rag/
  05-memory/
  06-frontend/
  07-testing/
  08-deploy/
  09-evals/
  10-codegen/
  11-roadmap/
```

## 强制边界

- MVP 不操作真实生产 Kubernetes。
- MVP 不做真实云资源写操作。
- L2/L3 动作必须人工审批。
- L4 动作直接拒绝，不能进入审批。
- 后端和前端测试覆盖率都必须高于 80%。
- Agent 单元测试必须使用 FakeLLM。
- token 缓存、多级记忆、上下文压缩必须从第一版 Agent 设计时就接入。
