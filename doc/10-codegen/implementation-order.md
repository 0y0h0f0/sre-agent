# 代码生成顺序

## 原则

每次生成一个窄模块，并立即生成测试。不要先生成复杂 Agent 再补底层结构。

## Step 1：基础设施

生成：

- `pyproject.toml`，作为 Python 依赖和工具配置的唯一来源。
- `package.json`，作为前端依赖和 npm scripts 的唯一来源。
- Docker Compose skeleton。
- settings、errors、ids、time utils。
- pytest、ruff、mypy、vitest 配置。

依赖管理约束：

- Python 不生成 Poetry、Pipenv 或多个 requirements 文件；使用 `pyproject.toml` + editable install。
- 推荐本地命令：`python -m pip install -e ".[dev]"`。如果环境使用 uv，可以等价执行 `uv pip install -e ".[dev]"`，但文档和脚本不能强依赖 uv。
- 前端固定使用 npm，不生成 pnpm-lock、yarn.lock 或 bun.lock。
- npm scripts 必须包含 `test:coverage` 和 `test:e2e`。

验收：lint 和空测试可运行。

## Step 2：数据库与 schema

生成：

- SQLAlchemy models。
- Alembic migration。
- Pydantic schemas。
- repository。

验收：数据库测试通过，覆盖率 > 80%。

## Step 3：基础 API 与 Celery

生成：

- health router。
- alerts router。
- incidents router。
- Celery app。
- diagnosis task stub。

验收：`POST /api/alerts` 可创建 incident 并入队。

## Step 4：工具层

生成：

- BaseTool。
- MetricsTool。
- LogsTool。
- TraceTool fake。
- GitChangeTool。
- Tool cache。

验收：工具单元和集成测试通过。

## Step 5：RAG

生成：

- Runbook splitter。
- metadata parser。
- embedding fake adapter。
- pgvector retriever。
- search API。

验收：Runbook ingest/search 通过。

## Step 6：Memory 与 Context

生成：

- token counter。
- prompt segment cache。
- memory store。
- context budgeter。
- compressor。
- context builder。

验收：缓存 key 稳定、超预算触发压缩、evidence id 保留。

## Step 7：LangGraph

生成：

- state。
- graph builder。
- 每个节点。
- FakeLLM adapter。
- parser 和 retry。

验收：4 类 fixture 能跑完诊断。

## Step 8：Guardrail 与审批

生成：

- policy。
- approval API。
- action API。
- mock executor。
- checkpoint resume。

验收：L2/L3 审批，L4 拒绝。

## Step 9：React UI

生成：

- API client。
- routes。
- incident pages。
- run timeline。
- approval dialog。
- report view。

验收：组件测试和 Playwright smoke 通过。

## Step 10：Eval 与包装

生成：

- eval dataset。
- runner。
- metrics。
- README。
- demo scripts。

验收：smoke eval 输出报告。
