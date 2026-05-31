# 模块生成检查清单

## 每个后端模块

- 有 Pydantic input/output schema。
- 有 service 或 repository 边界。
- 有单元测试。
- 有错误处理。
- 有 request_id 或 agent_run_id 追踪。
- 有覆盖率。

## 每个工具

- 有 query schema。
- 有 result schema。
- 有超时。
- 有重试或降级策略。
- 有 cache key。
- 有 tool_call 审计字段。
- 有 mock 测试。

## 每个 Agent 节点

- 输入输出只通过 `IncidentState`。
- 节点逻辑可单测。
- 大文本先摘要。
- evidence id 可追踪。
- 失败时写 `errors` 而不是直接吞掉。
- 不在节点中硬编码数据库 session。

## 每个 LLM 调用

- system prompt 稳定。
- output schema 固定。
- context 经过 budgeter。
- 大日志经过 compressor。
- prompt segment cache 可命中。
- JSON parse 失败有一次修复重试。
- 记录 token 和 cache hit/miss。

## 每个前端页面

- 有 loading state。
- 有 empty state。
- 有 error state。
- 有 API mock test。
- 不把长 JSON 原样铺满页面。
- pending 状态合理轮询。

## 每个文档对应代码

- `architecture.md` 对应目录和模块边界。
- `api-contract.md` 对应 router/schema/service 测试。
- `data-model.md` 对应 SQLAlchemy/Alembic。
- `langgraph-workflow.md` 对应 `packages/agent`。
- `tools.md` 对应 `packages/tools`。
- `runbook-rag.md` 对应 `packages/rag`。
- `token-cache-and-context.md` 对应 `packages/memory`。
- `testing-strategy.md` 对应 CI。
