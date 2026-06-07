# 文档质量门禁

本文件用于代码生成前的最终检查。Codex、Claude Code 或其他编程代理在开始生成代码前，应确认以下条件全部满足。

## 1. 技术栈固定

必须使用：

- FastAPI
- LangGraph
- Celery
- PostgreSQL
- pgvector
- Redis
- Prometheus
- Loki
- OpenTelemetry demo/mock trace
- React + TypeScript + Vite

不得替换为：

- OpenAI Agents SDK
- Dramatiq
- Elasticsearch
- Next.js
- Streamlit
- 真实生产 Kubernetes 写操作

## 2. 项目边界明确

- MVP 是单租户本地 demo。
- MVP 只覆盖 4 类故障。
- 动作执行只使用 mock executor。
- CI 和 smoke eval 只使用 FakeLLM。
- 真实 LLM 只允许手动 full eval 或手动 demo。
- L4 动作直接拒绝。

## 3. 结构清晰

代码生成必须遵守：

```text
apps/api        FastAPI routers, schemas, services
apps/worker     Celery app and tasks
apps/web        React console
packages/agent  LangGraph workflow, nodes, guardrails
packages/tools  Prometheus, Loki, trace, Git, mock executor tools
packages/rag    Runbook ingest, split, retrieve, rerank
packages/memory token cache, context budget, compression, memory store
packages/db     SQLAlchemy models, repositories, migrations
packages/evals  datasets, runner, metrics
```

不得把所有逻辑塞进单个 `main.py` 或单个 Agent 文件。

依赖管理必须清晰：

- Python 依赖只通过 `pyproject.toml` 管理。
- 前端依赖只通过 `package.json` 和 npm 管理。
- 不引入 Poetry、Pipenv、pnpm、yarn 或 bun，除非用户明确要求迁移。

## 4. API 契约完整

必须实现并测试：

- `GET /healthz`
- `GET /readyz`
- `POST /api/alerts`
- `GET /api/incidents`
- `GET /api/incidents/{incident_id}`
- `POST /api/incidents/{incident_id}/diagnose`
- `GET /api/incidents/{incident_id}/runs`
- `GET /api/agent-runs/{agent_run_id}`
- `GET /api/approvals`
- `GET /api/incidents/{incident_id}/approvals`
- `POST /api/approvals/{approval_id}/approve`
- `POST /api/approvals/{approval_id}/reject`
- `GET /api/actions/{action_id}`
- `POST /api/actions/{action_id}/execute`
- `POST /api/runbooks/ingest`
- `GET /api/runbooks/search`
- `GET /api/incidents/{incident_id}/report`
- `POST /api/incidents/{incident_id}/report/regenerate`

所有写接口必须处理 `X-Request-Id`。

## 5. 审批和安全无歧义

- L2 需要 approved approval。
- L3 需要 approved approval，并保存：
  - `risk_ack=true`
  - `confirm_action_type == action.type`
  - `confirm_target == action.target`
- L4 永远不能执行。
- 模型输出不能绕过 guardrail。

## 6. LangGraph checkpoint 无歧义

必须使用：

```python
from langgraph.checkpoint.postgres import PostgresSaver

config = {
    "configurable": {
        "thread_id": agent_run_id,
        "checkpoint_ns": "",
    }
}
```

- `thread_id` 固定为 `agent_run_id`。
- `agent_runs.state` 只做展示快照，不能替代 checkpoint。
- 审批恢复必须使用同一个 config。

## 7. RAG 和 embedding 无歧义

- `runbook_chunks.embedding` 使用 `vector(384)`。
- `memory_items.embedding` 使用 `vector(384) nullable`。
- FakeEmbedding 必须 deterministic。
- Runbook 检索结果必须包含 `chunk_id`、`source_path`、`title`、`excerpt`、`score`、`metadata`。
- 诊断结果必须引用 evidence id 或 chunk id。

## 8. Token 缓存和记忆无歧义

必须区分：

- Provider prompt cache：模型服务商前缀缓存指标。
- App prompt segment cache：系统自己的 Redis/application cache。

如果 provider 不返回缓存指标，标记为 `unknown`，不得用 Redis 命中率伪造 provider cache 命中率。

记忆层级：

- L0 run-local memory。
- L1 incident memory。
- L2 service memory。
- L3 procedural memory。

`packages/memory` 不直接调用 LLM provider。LLM 摘要由 `packages/agent` 通过 summarizer adapter 执行。

## 9. 测试门禁明确

后端：

```bash
pytest tests/unit tests/integration --cov=apps --cov=packages --cov-report=term-missing --cov-report=xml --cov-fail-under=80
```

前端：

```bash
npm run test:coverage
```

E2E：

```bash
npm run test:e2e
```

必须测试：

- fingerprint 去重。
- Celery 幂等。
- checkpoint resume。
- L3 二次确认。
- L4 直接拒绝。
- FakeEmbedding deterministic。
- context compression 保留 evidence id。
- provider/app cache 指标拆分。

## 10. 代码生成前检查

开始生成代码前，代理应确认：

- 没有把 MVP 外功能当成必做项。
- 没有引入被排除的技术栈。
- 没有真实 destructive action。
- 没有依赖真实 LLM 才能通过测试。
- 没有未定义的前端页面依赖接口。
- 没有无法落库或无法测试的 Agent 状态。

若发现冲突，优先遵守 `AGENTS.md`、`plans/10-codegen/documentation-quality-gate.md` 和 `docs/` 中对应模块的当前文档。
