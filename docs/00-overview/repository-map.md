# 仓库地图

## 顶层结构

```text
apps/
  api/
  worker/
  web/
packages/
  agent/
  common/
  db/
  evals/
  memory/
  rag/
  tools/
demo/
  alerts/
  demo_service/
  faults/
  runbooks/
  topology.json
deploy/
  bge-zh.Dockerfile
  bge_zh_server.py
migrations/
plans/
tests/
docs/
```

## `apps/api`

FastAPI 应用入口和 HTTP 层。

- `main.py`：创建 app、注册 middleware、异常处理、routers。
- `dependencies.py`：数据库 session、服务依赖。
- `routers/`：路由函数，保持薄层。
- `schemas/`：Pydantic 请求和响应模型。
- `services/`：业务逻辑。
- `middleware/auth.py`：API key 鉴权。
- `ws/`：WebSocket 节点事件订阅和 Redis publisher。

## `apps/worker`

Celery 任务。

- `celery_app.py`：Celery 配置、beat schedule、worker metrics server。
- `tasks.py`：诊断、审批恢复、邮件通知、每日摘要、自动审批任务。
- `eval_tasks.py`：异步评测相关任务。

## `apps/web`

React + TypeScript + Vite 控制台。

- `src/App.tsx`：主路由和页面实现。
- `src/api.ts`：API client。
- `src/styles.css`：全局样式。
- `src/e2e/`：Playwright smoke spec。
- `package.json`：前端 scripts 和依赖。

## `packages/agent`

LangGraph Agent。

- `graph.py`：StateGraph 构建和条件路由。
- `runner.py`：run/resume 入口。
- `state.py`：`IncidentState`。
- `schemas.py`：Agent 输出 schema 和 `AgentDeps`。
- `nodes/`：每个节点的测试able Python 函数。
- `guardrails/`：确定性风险分类和审批策略。
- `llm/`：Fake/OpenAI/DeepSeek/Anthropic/vLLM 等 provider adapter，含 `reasoning.py` 推理节点支持。
- `evidence_validation.py`：证据交叉验证，融合 metrics/logs/traces/deployment 信号。
- `topology.py`：服务依赖图和级联故障分析。
- `prompts.py`：稳定 prompt 模板。

## `packages/tools`

工具层。

- `metrics.py`：Prometheus metrics tool。
- `logs.py`：Loki logs tool。
- `traces.py`、`trace_backends.py`：Trace 工具和后端。
- `git_changes.py`、`deployment_backends.py`：部署变更工具和后端。
- `k8s.py`：Kubernetes 只读诊断。
- `db_diagnostics.py`：数据库只读诊断。
- `runbook_search.py`：RAG wrapper tool。
- `mock_executor.py`：MVP mock executor。
- `cache.py`：请求级工具缓存和稳定 cache key。
- `base.py`：通用 ToolResult/BaseTool。

## `packages/rag`

Runbook RAG。

- `ingest.py`：Markdown runbook 入库。
- `splitter.py`：chunk 切分。
- `embeddings.py`、`embedding_factory.py`：Fake / BGE-ZH / text2vec embedding provider。
- `retriever.py`：向量/词法/BM25 混合检索。
- `bm25.py`：tsquery 和 BM25 score helper。
- `reranker.py`、`reranker_backends.py`：rerank score 和后端。
- `runbook_generator.py`：Runbook 草稿生成。
- `metadata.py`、`template_extractor.py`：metadata 和模板抽取。

## `packages/memory`

记忆、缓存和上下文预算。

- `context_budget.py`：预算定义。
- `context_builder.py`：构建 prompt context。
- `compressor.py`：确定性压缩。
- `memory_store.py`：`memory_items` 和 `memory_events` 访问。
- `schemas.py`：memory schema。
- `token_counter.py`：token 粗估。

## `packages/db`

数据访问层。

- `models.py`：SQLAlchemy model。
- `session.py`：engine、SessionLocal。
- `repositories/`：仓储类。
- `base.py`：declarative base。

## `tests`

- `unit/`：模块级单元测试。
- `integration/`：API、worker、graph、RAG 等集成测试。
- `contract/`：API contract 测试。
- `manual/`：真实邮件手动测试，默认跳过。

## `plans` 与 `docs`

- `plans/`：设计来源、roadmap、代码生成约束。
- `docs/`：当前生成的项目说明、运行、开发、运维和参考文档。
