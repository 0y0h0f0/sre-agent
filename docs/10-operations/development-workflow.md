# 开发工作流

**最后更新：** 2026-06-14

本文描述当前仓库的日常开发路径：从读文档、选测试层级、实现变更，到运行 CI 等价检查和更新文档。实现细节冲突时，优先级是当前代码和 `docs/` 专题文档高于历史 `plans/`。

## 开始前阅读

| 变更范围 | 先读 |
|----------|------|
| 架构/边界 | [架构](../00-overview/architecture.md)、[范围与边界](../00-overview/scope-and-boundaries.md) |
| API/服务/DB | [后端架构](../01-backend/backend-architecture.md)、[API 参考](../01-backend/api-reference.md)、[数据模型](../01-backend/data-model.md) |
| Celery/worker | [Celery 与任务](../01-backend/celery-and-jobs.md) |
| Agent 节点/路由 | [Agent 工作流](../02-agent/workflow.md) |
| Guardrail/approval/executor | [护栏与审批](../02-agent/guardrails-and-approval.md) |
| Tool backend | [工具层](../03-tools/tool-layer.md) |
| Runbook/RAG | [Runbook RAG](../04-rag/runbook-rag.md) |
| Memory/cache/compression | [记忆、缓存与压缩](../05-memory/memory-cache-compression.md) |
| Frontend | [React 控制台](../06-frontend/react-console.md) |
| Tests/evals | [测试策略](../07-testing/testing-strategy.md)、[评测体系](../09-evals/evaluation.md) |
| Local demo | [快速开始](../00-overview/quick-start.md)、[本地部署](../08-deploy/local-demo.md) |

## 本地环境

安装后端依赖：

```bash
python -m pip install -e ".[dev]"
```

推荐先用 Compose 跑完整本地环境：

```bash
docker compose up -d
```

完整 Compose 的 `api` 容器会执行 `alembic upgrade head`。如果只让 Compose 提供依赖，在宿主机手动跑 API/worker，要使用映射端口：PostgreSQL `localhost:5433`，Redis `localhost:6378`。

手动开发常用环境变量：

```bash
export DATABASE_URL=postgresql+psycopg://sre:sre@localhost:5433/sre
export REDIS_URL=redis://localhost:6378/0
export CELERY_BROKER_URL=redis://localhost:6378/1
export CELERY_RESULT_BACKEND=redis://localhost:6378/2
export LLM_PROVIDER=fake
export EMBEDDING_PROVIDER=fake
export RERANKER_PROVIDER=fake
export TRACE_BACKEND=fixture
export DEPLOYMENT_BACKEND=fixture
export K8S_BACKEND=fixture
export DB_DIAGNOSTICS_BACKEND=fixture
export EXECUTOR_BACKEND=fixture
export API_KEY_AUTH_ENABLED=false
```

手动启动：

```bash
alembic upgrade head
uvicorn apps.api.main:app --reload --port 8000
celery -A apps.worker.tasks:celery_app worker --loglevel=INFO
```

前端：

```bash
cd apps/web
npm ci
VITE_API_BASE_URL=http://localhost:8000 npm run dev
```

## 实现流程

1. 明确变更属于哪个模块，读取对应专题文档。
2. 写或更新最小失败测试。
3. 保持 router/service/repository、node/tool、schema/model 边界清楚。
4. 默认使用 FakeLLM、fixture executor、fixture tools；除非任务明确要求，不接入真实外部写路径。
5. 运行聚焦测试。
6. 运行 CI 等价检查中与变更相关的部分。
7. 行为或配置发生变化时更新 `docs/`。
8. 最后检查安全边界：L2/L3/L4、M9 feature gate、secret redaction、生产默认值。

## CI 等价命令

后端：

```bash
ruff check apps packages tests
mypy apps packages
pytest tests/unit tests/integration --cov=apps --cov=packages --cov-report=term-missing --cov-fail-under=80
python -m packages.evals.runner --suite smoke --output reports/eval-smoke.json
```

前端：

```bash
cd apps/web
npm run test:coverage
npm run build
npx playwright install --with-deps chromium
npm run test:e2e
```

`.github/workflows/ci.yml` 当前运行 Python 3.11 和 Node 20。CI 不默认运行 Python `tests/e2e/`、contract、manual 或 full eval；这些由变更风险决定是否额外执行。

## 测试选择

| 变更 | 推荐命令 |
|------|----------|
| Guardrail 风险规则 | `pytest tests/unit/test_guardrails.py -v` |
| Approval/API resume | `pytest tests/integration/test_approval_api.py -v` |
| Alert ingestion/fingerprint | `pytest tests/integration/test_alert_api.py -v` |
| Worker/Celery | `pytest tests/integration/test_worker_task.py -v` |
| Agent 节点 | `pytest tests/unit/test_agent_nodes.py -v` + `pytest tests/integration/test_graph_flow.py -v` |
| Tool layer | `pytest tests/unit/test_tools.py tests/unit/test_tools_phase2.py -v` |
| RAG | `pytest tests/unit/test_rag.py tests/integration/test_runbook_api.py -v` |
| Memory/compression | `pytest tests/unit/test_memory.py -v` |
| Eval runner | `pytest tests/integration/test_eval_runner.py -v` |
| Frontend API/client | `cd apps/web && npm run test:coverage` |
| Browser smoke | `cd apps/web && npm run test:e2e` |
| Production safety | `pytest tests/unit/test_production_safety.py tests/unit/test_backend_url_safety.py -v` |
| M9 gates/safety | `pytest tests/unit/test_m9_feature_flags.py tests/unit/test_web_search_safety.py -v` |

更多选择规则见 [测试策略](../07-testing/testing-strategy.md)。

## 数据库迁移

生成迁移：

```bash
alembic revision --autogenerate -m "describe change"
```

应用迁移：

```bash
alembic upgrade head
```

迁移变更要求：

- SQLAlchemy model、migration、repository、schema 文档同步。
- 新 JSON/vector 字段需要明确 nullable、默认值和降级路径。
- 不在迁移中写入真实生产业务数据。
- 发布前至少验证 upgrade；高风险 migration 还要验证 downgrade 或写明不可逆原因。

## 依赖管理

后端依赖只通过根 `pyproject.toml` 管理。不要新增 Poetry、Pipenv 或 requirements 文件。

前端依赖只通过 `apps/web/package.json` 和 npm lockfile 管理。不要新增 pnpm/yarn/bun lockfile。

新增依赖前确认：

- 是否已有标准库或项目内 helper 能满足需求。
- 是否会引入真实外部调用、后台线程或不可控网络行为。
- 是否影响 CI 安装时间和离线可测试性。
- 是否需要新增安全配置或 feature gate。

## 前端开发规则

- 使用 `api.ts` 增加 typed API helper，不在组件中散落 fetch。
- 使用 TanStack Query key，mutation 成功后失效相关 query。
- 覆盖 loading、empty、error、401 和审批冲突/校验失败状态。
- L3 审批 UI 必须显式采集 `risk_ack`、`confirm_action_type`、`confirm_target`。
- 页面路由变更同步 [React 控制台](../06-frontend/react-console.md)。

## 安全边界

必须保持：

- `POST /api/alerts` 只创建 incident/run 并入队 Celery，不内联跑 LangGraph。
- CI 和默认本地 demo 使用 FakeLLM。
- 默认 executor 是 `fixture`。
- `EXECUTOR_BACKEND=live` 是显式选择加入，只允许 restart/scale/rollback 三类受控 K8s mutation。
- live K8s diagnostics 和 live DB diagnostics 均只读。
- L2/L3 需要审批，L3 需要二次确认，L4 直接拒绝。
- M9 子能力受 `M9_EXTENSIONS_ENABLED` 和独立开关共同控制；生产默认关闭。
- 原始 secret 不进 DB、audit、logs、prompt、Agent state。

## PR 检查清单

- [ ] 变更范围对应的专题文档已阅读。
- [ ] 新行为有 unit/integration/contract/E2E 中合适层级的测试。
- [ ] `ruff check apps packages tests` 通过。
- [ ] `mypy apps packages` 通过，或已说明遗留类型问题。
- [ ] 后端覆盖率命令通过或已说明未运行原因。
- [ ] 前端相关变更运行 `npm run test:coverage`；跨页面变更运行 `npm run test:e2e`。
- [ ] smoke eval 仍使用 FakeLLM。
- [ ] 未引入真实 destructive action、真实 cloud write 或默认开启的外部调用。
- [ ] 行为、API、配置、部署或测试命令变化已更新文档。
