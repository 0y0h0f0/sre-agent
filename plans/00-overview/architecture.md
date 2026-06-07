# 系统架构

## 目标

系统在收到报警后，自动创建 incident，异步触发 LangGraph 诊断流程，收集指标、日志、trace、Git 变更和 Runbook，输出根因、证据、处置建议、审批动作和复盘报告。

## 固定技术栈

- API：FastAPI。
- Agent 编排：LangGraph。
- 异步任务：Celery。
- 数据库：PostgreSQL。
- 向量库：pgvector。
- 队列与短期状态：Redis。
- 指标：Prometheus。
- 日志：Loki。
- Trace：OpenTelemetry demo 数据或 mock 数据。
- 前端：React + TypeScript + Vite。

## 主链路

```text
Alertmanager / Mock Alert
        |
        v
FastAPI POST /api/alerts
        |
        v
PostgreSQL incident + agent_run
        |
        v
Celery run_incident_diagnosis
        |
        v
LangGraph workflow
        |
        +--> MetricsTool -> Prometheus
        +--> LogsTool    -> Loki
        +--> TraceTool   -> OTel mock/demo data
        +--> GitTool     -> demo git changes
        +--> RAG         -> pgvector runbook chunks
        |
        v
Diagnosis + Evidence + Actions
        |
        +--> L0/L1 auto execute or persist
        +--> L2/L3 interrupt and wait approval
        +--> L4 reject directly
        |
        v
Incident Report + UI display + Eval metrics
```

## 运行时模块

| 模块 | 代码位置 | 责任 |
| --- | --- | --- |
| API | `apps/api` | 接收请求、校验参数、入队任务、查询状态 |
| Worker | `apps/worker` | 启动 Celery worker，执行诊断和评测任务 |
| Agent | `packages/agent` | LangGraph 状态机、节点、prompt、guardrail |
| Tools | `packages/tools` | Prometheus、Loki、Trace、Git、Action executor |
| RAG | `packages/rag` | Runbook 切分、embedding、检索、rerank |
| Memory | `packages/memory` | token cache、多级记忆、上下文压缩 |
| DB | `packages/db` | SQLAlchemy model、session、repository、migration |
| Evals | `packages/evals` | 样本加载、指标计算、报告输出 |
| Web | `apps/web` | React 控制台 |
| Demo | `demo` | demo-service、故障注入、Runbook、alert fixture |

## 关键设计决定

1. FastAPI 不直接执行诊断，只落库并入队 Celery。
2. LangGraph 节点必须是普通 Python 函数，方便单元测试。
3. 每次工具调用必须写入 `tool_calls`，包括耗时、输入摘要、输出摘要和错误。
4. 所有 LLM 输入必须经过上下文预算器，避免无边界塞入日志和文档。
5. RAG 返回内容必须带来源，诊断结果不能只有模型自由推断。
6. 审批通过后从 LangGraph PostgreSQL checkpointer 恢复，Python 实现使用 `langgraph.checkpoint.postgres.PostgresSaver`，`thread_id` 固定为 `agent_run_id`，不能重新执行已完成的危险动作。

## 数据流

1. `POST /api/alerts` 创建 `incidents` 和 `agent_runs`。
2. Celery task 读取 incident，使用 LangGraph PostgreSQL checkpointer 启动 graph，config 固定为 `{configurable: {thread_id: agent_run_id, checkpoint_ns: ""}}`。
3. 每个节点读取和更新 Agent state。
4. 工具层查询外部或 demo 数据，并写 `tool_calls`、`evidence_items`。
5. 诊断节点生成 `hypotheses` 和 `root_cause`。
6. 处置节点生成 `actions`。
7. guardrail 节点决定自动执行、等待审批或拒绝。
8. 报告节点生成 `incident_reports`。
9. React 通过 API 展示 incident、run、approval、report。

## 代码生成注意事项

- 先定义 Pydantic schema，再实现 router，最后实现 service/repository。
- 工具接口先实现协议和 fake 实现，再接 HTTP client。
- Agent 节点先用 deterministic fixture 测通，再接真实 LLM adapter。
- 所有 ID 使用统一生成器，如 `inc_`、`run_`、`tool_`、`act_` 前缀。
- 时间统一使用 UTC ISO 8601。
