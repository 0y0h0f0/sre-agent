# 项目概览

## 目标

SRE Incident Response Agent 用于本地 demo 环境中的事故响应自动化。系统接收告警后创建事故记录，异步运行 LangGraph 诊断流程，汇总可追溯证据，输出根因、处置建议、风险分类、审批请求和事故报告。

## 使用场景

- 演示一个 SRE Agent 如何从告警进入诊断、审批和报告闭环。
- 验证 Runbook RAG、上下文压缩、多级记忆和工具审计的工程边界。
- 使用 FakeLLM 和 fixture 数据稳定测试事故诊断链路。
- 在本地环境中演示人工审批如何阻断高风险动作。

## 固定技术栈

| 层 | 技术 |
| --- | --- |
| API | Python 3.11+、FastAPI、Pydantic |
| 数据库 | PostgreSQL、SQLAlchemy、Alembic、pgvector |
| 异步任务 | Celery、Redis broker/result backend |
| Agent | LangGraph、PostgreSQL checkpointer |
| 观测数据 | Prometheus、Loki、OpenTelemetry demo/fixture |
| RAG | Runbook chunk、FakeEmbedding / BGE-ZH / text2vec、pgvector、BM25、reranker |
| 前端 | React、TypeScript、Vite、TanStack Query |
| 测试 | pytest、pytest-cov、Vitest、React Testing Library、Playwright |

## MVP 支持的事故类型

1. database connection exhaustion
2. high 5xx after deploy
3. Redis cache avalanche
4. Pod restart loop with mock Kubernetes events

这些类型的 alert fixture 位于 `demo/alerts/`，对应 Runbook 位于 `demo/runbooks/checkout-api/`。

## 已完成的扩展能力

按当前项目收口口径，MVP 主链路和已纳入仓库的 post-MVP roadmap 能力均已完成。扩展能力包括：

- LLM provider factory：`fake`、`openai`、`deepseek`、`anthropic`、`vllm` 等适配入口。
- LLM reasoning：可配置的深度推理节点（`LLM_REASONING_ENABLED` / `LLM_REASONING_NODES`），输出 `diagnosis_rationale` 和 LLM 调用元数据。
- 证据交叉验证：metrics/logs/traces/deployment 信号融合，corroboration 提高置信度，冲突触发人工审查。
- 级联故障分析：基于服务依赖图的故障传播分析、根服务识别、关联事故聚类。
- 更完整的工具后端：Trace、Git/deployment、K8s、DB diagnostics 支持 fixture 或可选真实读后端。
- 邮件通知：事故完成、审批请求、报告、每日摘要。
- Runbook 增强：BM25 混合检索、reranker、多语言 embedding 配置（BGE-ZH / text2vec）、草稿和版本。
- 记忆与学习：NFA 标记、跨事故关联、反馈数据。
- 协作：评论、证据标注、审批组、审计日志。
- Ops：API key 鉴权、Prometheus metrics、WebSocket 节点事件、shadow eval、Celery beat 周期任务。

这些扩展默认保持本地 demo 可复现，不突破 mock executor 和安全边界。真实 provider 或真实只读后端需要显式配置，不能成为 CI 稳定门禁。

## 主要输出

- `incidents`：事故列表和详情。
- `agent_runs`：诊断运行记录、节点轨迹、token/cache 指标。
- `evidence_items`：指标、日志、trace、部署、Runbook、记忆等证据。
- `actions`：推荐动作和执行结果。
- `approvals`：L2/L3 审批请求和决策。
- `incident_reports`：可版本化的事故报告。
- `tool_calls`：工具调用审计记录。

## 非目标

- 不做生产级 Kubernetes 写操作。
- 不执行真实云资源变更。
- 不删除数据、truncate table、flush 真实缓存或修改真实数据库。
- 不把真实 LLM 作为 CI 稳定门禁。
- 不把 roadmap 中的 RBAC/SSO、模型微调、真实自动化执行当成 MVP 范围。
