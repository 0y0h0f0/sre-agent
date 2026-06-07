# 开发与验收流程

## 开发顺序

默认实现顺序：

1. Project scaffolding and tool configuration。
2. Shared settings、errors、IDs、time helpers。
3. Database models、migrations、repositories。
4. Pydantic schemas。
5. FastAPI routers and services。
6. Celery app and task stubs。
7. Tool layer with fake/mockable HTTP clients。
8. Runbook RAG。
9. Memory、token cache、context budgeting、compression。
10. LangGraph state、nodes、runner、FakeLLM。
11. Guardrail、approval、checkpoint resume、mock executor。
12. React console。
13. Tests、E2E、eval runner、README/demo scripts。

如果用户明确要求某个 slice，可按请求调整，但不能突破安全边界。

## 变更原则

- 先读对应 `plans/` 文档和当前代码，再实现。
- 优先沿用现有目录、schema、repository、service 和测试风格。
- Router 保持薄层。
- 业务逻辑放 service。
- 数据访问放 repository。
- 新工具必须有 query schema、result schema、timeout、degraded behavior、cache key、audit summary 和测试。
- 新 Agent 节点必须是普通可测试函数，通过 `AgentDeps` 注入依赖。
- 新高风险行为必须先更新 guardrail 和审批测试。

## 依赖管理

Python：

- 只通过根 `pyproject.toml` 管理依赖。
- 推荐安装：`python -m pip install -e ".[dev]"`。
- 不新增 Poetry、Pipenv 或 requirements 文件。

Frontend：

- 只通过 `apps/web/package.json` 和 npm 管理依赖。
- 不新增 pnpm、yarn、bun lockfile。
- 必须保留 `test:coverage` 和 `test:e2e` scripts。

## 数据库变更流程

1. 修改 `packages/db/models.py`。
2. 新增 Alembic migration。
3. 更新 repository。
4. 更新 schema/service/API。
5. 增加 unit/integration 测试。
6. 更新 `docs/01-backend/data-model.md` 和相关 API 文档。

注意：

- public ID 不使用数据库自增 ID。
- 新 embedding 字段必须明确维度。
- report version 不覆盖旧记录。
- checkpoint 字段不能替代 LangGraph checkpointer。

## API 变更流程

1. 定义或更新 Pydantic schema。
2. 更新 service。
3. 更新 router。
4. 增加 integration 或 contract 测试。
5. 更新 `docs/01-backend/api-reference.md`。
6. 如前端使用，更新 `apps/web/src/api.ts` 类型和页面测试。

错误响应必须保持标准 envelope。

## Agent 变更流程

1. 明确 state 字段。
2. 节点函数接收 `state` 和 `deps`。
3. 节点写 node trace。
4. 工具调用写 tool call。
5. 大日志不直接进入 prompt。
6. 诊断输出保留 evidence ID。
7. 更新 graph edges 和条件路由。
8. 增加节点单元测试和 graph flow 测试。
9. 更新 `docs/02-agent/workflow.md`。

## Guardrail 变更流程

1. 在确定性 policy 中添加 action type。
2. 明确 L0-L4。
3. 如果 L2/L3，增加审批测试。
4. 如果 L3，测试二次确认。
5. 如果 L4，测试 direct reject。
6. 确认 mock executor 不执行 L4。
7. 更新 `docs/02-agent/guardrails-and-approval.md`。

## 前端变更流程

1. 更新 `src/api.ts` 类型和 client 函数。
2. 页面使用 TanStack Query。
3. 覆盖 loading、empty、error、success。
4. active 状态需要 polling 或 WebSocket invalidation。
5. L3 UI 必须包含二次确认。
6. 添加 React Testing Library 测试。
7. 需要跨页面验证时添加 Playwright。
8. 更新 `docs/06-frontend/react-console.md`。

## 验收清单

每个 coding task 完成时确认：

- 代码在正确模块中。
- 单元测试已添加或更新。
- 跨模块行为有 integration/contract/E2E 测试。
- 覆盖率门禁预期通过。
- 没有 MVP 边界违规。
- docs 已更新。
- FakeLLM 仍用于 CI。
- mock executor 仍是 MVP 执行路径。
- L2/L3 仍审批，L4 仍拒绝。

## 推荐最终命令

后端：

```bash
pytest tests/unit tests/integration --cov=apps --cov=packages --cov-report=term-missing --cov-report=xml --cov-fail-under=80
ruff check apps packages tests
mypy apps packages
```

前端：

```bash
cd apps/web
npm run test:coverage
npm run test:e2e
npm run build
```

评测：

```bash
python -m packages.evals.runner --suite smoke
```

## 文档更新规则

- 行为变化更新 `docs/`。
- 规划变化更新 `plans/`。
- API 变化更新 API reference。
- 配置变化更新 configuration。
- 表结构变化更新 data model。
- 安全策略变化更新 guardrail 文档。
