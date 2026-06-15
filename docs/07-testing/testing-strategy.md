# 测试策略

**最后更新：** 2026-06-15

测试的目标不是只追求覆盖率数字，而是持续证明三件事：事件响应主链路正确、风险边界不会被绕过、默认 local/CI 路径保持确定性。CI、单元测试、集成测试和 smoke eval 必须使用 FakeLLM、fixture/mock 后端和可复现数据。

## 当前测试资产

Python 测试文件按目录分布：

| 层级 | 文件数 | 位置 | CI 默认运行 | 主要用途 |
|------|--------|------|-------------|----------|
| Unit | 71 | `tests/unit/` | 是 | 纯函数、节点、工具、RAG、memory、配置、安全规则 |
| Integration | 22 | `tests/integration/` | 是 | FastAPI、repository、Celery task、API auth、report、eval runner |
| Python E2E | 4 | `tests/e2e/` | 否 | TestClient 级端到端 smoke、M9 专项端到端结构测试 |
| Contract | 1 | `tests/contract/` | 否 | API response shape 契约，如 runbook search result 字段 |
| Manual | 2 | `tests/manual/` | 否 | 真实 SMTP 手动测试，受环境变量保护 |
| Total | 100 | `tests/**/test*.py` | 部分 | 不含 `tests/conftest.py` |

前端测试：

| 层级 | 文件 | 当前测试数 | CI 默认运行 |
|------|------|------------|-------------|
| Page/API unit | `apps/web/src/App.test.tsx` | 19 | 是，`npm run test:coverage` |
| API client unit | `apps/web/src/api.test.ts` | 11 | 是，`npm run test:coverage` |
| Browser smoke | `apps/web/src/e2e/smoke.spec.ts` | 1 | 是，`npm run test:e2e` |

## CI 门禁

`.github/workflows/ci.yml` 当前有两个 job。

Backend job：

```bash
python -m pip install -e ".[dev]"
ruff check apps packages tests
mypy apps packages
pytest tests/unit tests/integration --cov=apps --cov=packages --cov-report=term-missing --cov-fail-under=80
python -m packages.evals.runner --suite smoke --output reports/eval-smoke.json
```

Frontend job：

```bash
cd apps/web
npm ci
npm run test:coverage
npm run build
npx playwright install --with-deps chromium
npm run test:e2e
```

CI 不默认运行 `tests/e2e/`、`tests/contract/`、`tests/manual/` 或 manual full eval。涉及跨模块主链路、API response shape、生产发布或 M9 rollout 的变更，应在 PR 说明中列出这些额外验证。

## 覆盖率

硬门禁：

| 范围 | 当前执行方式 | 阈值 |
|------|--------------|------|
| 后端总体 | pytest-cov，`--cov=apps --cov=packages` | 80% |
| 前端 statements/branches/functions/lines | Vitest coverage thresholds | 80% |

项目质量目标：

| 范围 | 目标 |
|------|------|
| `packages/agent`、`packages/tools`、`packages/rag`、`packages/db` | 关注 85%+，特别是行为变更涉及的文件 |
| `packages/agent/guardrails` | 关注 95%+，风险分类变更必须加精确测试 |

`pyproject.toml` 的 coverage 配置开启 branch coverage，source 为 `apps` 和 `packages`，并排除 `migrations/*`、`tests/*`、`packages/evals/*`。因此 eval runner 自身通过集成测试保证行为，不计入后端覆盖率分母。

## 固定测试默认值

`tests/conftest.py` 的 autouse fixture 会在每个测试中强制：

- `API_KEY_AUTH_ENABLED=false`
- `LLM_PROVIDER=fake`
- `EMBEDDING_PROVIDER=fake`

常用 fixture：

| Fixture | 行为 |
|---------|------|
| `db_session` | SQLite in-memory，`StaticPool`，每个测试创建/销毁全部 metadata |
| `test_settings` | SQLite + memory Redis URL + `CELERY_TASK_ALWAYS_EAGER=True` + fake LLM/embedding |
| `client` | FastAPI TestClient，override DB、task enqueue、resume enqueue、notification enqueue、settings |
| `fake_enqueue` | 记录 `(incident_id, agent_run_id)`，不真正入 Celery |
| `fake_resume_enqueue` | 记录 `(agent_run_id, decision)` |
| `fake_notification_enqueue` | 记录 notification enqueue payload |

注意：并非所有底层单元测试都会使用 `test_settings`，所以需要显式验证 Celery、Redis、Postgres 或 checkpointer 行为时，要在测试中清楚设置对应 dependency。

## 选择测试层级

| 变更类型 | 最小测试 | 需要扩展到 |
|----------|----------|------------|
| 纯函数、schema、配置解析 | Unit | 涉及 API 输入/输出时加 integration |
| SQLAlchemy model/repository/迁移语义 | Unit repository + integration API | schema 或约束变更时加 contract |
| FastAPI endpoint | Integration | UI 使用该接口时加 frontend API test |
| Celery task、worker idempotency、checkpoint | Integration | 主链路变更时加 Python E2E 或 smoke eval |
| LangGraph node/route | Unit node test | 影响 end-to-end phase 时加 `test_graph_flow.py` 或 eval |
| Guardrail/approval/executor | Unit guardrail + integration approval/action | L3/L4 或 live executor 变更必须加负向测试 |
| Tool backend | Unit with mocked clients/fixtures | 影响 Agent evidence 时加 integration/tool audit |
| RAG ingest/search | Unit + integration API | Response shape 变更加 contract |
| Memory/compression/token cache | Unit | 影响 Agent state/report 时加 integration/eval |
| React UI | `App.test.tsx` 或 `api.test.ts` | 跨页面主流程加 Playwright smoke |
| M9 feature | Unit feature gate/safety tests | 涉及 API 或 rollout 时加 Python E2E M9 tests |
| 生产配置/发布门禁 | Unit production safety + integration auth/config | 发布前按 checklist 手动验证 |

## 必测行为映射

| 行为 | 代表测试 |
|------|----------|
| Fingerprint 去重 | `tests/integration/test_alert_api.py`、`tests/e2e/test_e2e_flows.py`、`tests/unit/test_repositories.py` |
| Alertmanager poll 指纹与 resolved 推断 | `tests/integration/test_poll_integration.py`、`tests/unit/test_resolved_inference.py` |
| Celery 诊断任务幂等 | `tests/integration/test_worker_task.py` |
| Checkpointer fail-closed / SQLite no-checkpointer 路径 | `tests/integration/test_worker_task.py` |
| Graph flow 和 approval interrupt | `tests/integration/test_graph_flow.py` |
| Approval resume 和多审批批次 | `tests/integration/test_approval_api.py` |
| L3 二次确认 | `tests/integration/test_approval_api.py`、`apps/web/src/App.test.tsx`、`apps/web/src/api.test.ts` |
| L4 直接拒绝 | `tests/unit/test_guardrails.py`、`tests/integration/test_approval_api.py` |
| Unknown action 保守处理 | `tests/unit/test_guardrails.py` |
| No-checkpointer 不自动批准 L3 | `tests/unit/test_agent_nodes.py` |
| Replan bounded cap | `tests/unit/test_agent_nodes.py` |
| Verify/replan gate dispatch、K8s rollout、DB read-only gate | `tests/unit/test_agent_nodes.py` |
| Tool cache hit/miss | `tests/unit/test_tools.py`、`tests/unit/test_rag.py`、eval metrics |
| FakeEmbedding determinism | `tests/unit/test_rag.py` |
| Runbook search shape | `tests/integration/test_runbook_api.py`、`tests/contract/test_runbook_api_contract.py` |
| Evidence IDs after persistence/compression | `tests/unit/test_collect_all_evidence.py`、`tests/unit/test_memory.py`、`tests/unit/test_reasoning_layering.py` |
| Provider/app cache metrics separation | `tests/integration/test_eval_runner.py` |
| Production safety defaults | `tests/unit/test_production_safety.py`、`tests/unit/test_settings_production_defaults.py` |
| Backend URL SSRF safety | `tests/unit/test_backend_url_safety.py` |
| Live executor scope and K8s name validation | `tests/unit/test_executor_backends.py` |
| Config/API key scope enforcement | `tests/integration/test_config_api_auth.py`、`tests/integration/test_override_api_auth.py` |
| M9 feature gates and conflicts | `tests/unit/test_m9_feature_flags.py` |
| M9 Web/LLM/embedding secret safety | `tests/unit/test_web_search_safety.py`、`tests/unit/test_llm_runbook_generation.py`、`tests/unit/test_external_embedding_provider.py` |

## Backend 命令

常规 CI 等价命令：

```bash
ruff check apps packages tests
mypy apps packages
pytest tests/unit tests/integration --cov=apps --cov=packages --cov-report=term-missing --cov-fail-under=80
python -m packages.evals.runner --suite smoke --output reports/eval-smoke.json
```

开发时常用命令：

```bash
pytest tests/unit/test_guardrails.py -v
pytest tests/integration/test_approval_api.py -v
pytest tests/integration/test_worker_task.py -v
pytest tests/contract/test_runbook_api_contract.py -v
pytest tests/e2e/test_e2e_flows.py -v
```

不要把真实 LLM、真实 cloud provider、真实 Kubernetes mutation 或真实数据库写操作放进 CI。真实 provider 或 live backend 只能做手动 demo/full eval，并且结果不能作为稳定门禁。

## Frontend 命令

```bash
cd apps/web
npm run test:coverage
npm run build
npm run test:e2e
```

Vitest 使用 jsdom，coverage 阈值 statements/branches/functions/lines 全部 80%。Playwright smoke 会自动启动 Vite `:5173`，baseURL 为 `http://127.0.0.1:5173`。

前端新增或修改功能时：

- API client 变更更新 `api.test.ts`。
- 页面交互、loading/empty/error/401/L3 确认更新 `App.test.tsx`。
- 跨页面主流程、审批弹窗、报告路径变化更新 `src/e2e/smoke.spec.ts`。

## M9 测试规则

M9 能力全部由 feature gate 控制，测试必须覆盖 default-off、global gate forcing、sub-feature enabled、conflict warning/metric、secret redaction、external call degradation。

代表文件：

- `tests/unit/test_m9_feature_flags.py`
- `tests/unit/test_llm_runbook_generation.py`
- `tests/unit/test_incident_diff_analysis.py`
- `tests/unit/test_web_search_safety.py`
- `tests/unit/test_tempo_trace_backend.py`
- `tests/unit/test_grafana_alert_parser.py`
- `tests/unit/test_semantic_runbook_search.py`
- `tests/unit/test_external_embedding_provider.py`
- `tests/e2e/test_m9_ai_extensions.py`
- `tests/e2e/test_m9_tempo_grafana.py`
- `tests/e2e/test_m9_semantic_search.py`

这些测试可以验证 M9 行为和 API 形状，但 M9 外部调用不能成为 CI 稳定依赖。外部 provider 测试必须可 mock、可降级、可离线运行。

## Manual 测试

`tests/manual/` 用于真实 SMTP 连通性和真实邮件发送。它们不属于 CI 默认路径。真实邮件测试必须显式设置环境变量，例如 `RUN_REAL_EMAIL_TEST=true`，并确认不会向生产用户发送测试邮件。

## 新增测试 Checklist

- 测试名描述行为，不只描述实现细节。
- 先写失败测试，再实现或修正文档描述。
- 不使用随机向量、随机时间或真实网络作为断言基础。
- 需要时间的测试使用固定 UTC 时间或 monkeypatch。
- 涉及风险等级时同时覆盖正向和负向路径。
- 涉及审批时覆盖 L2、L3 缺字段、L3 字段不匹配、reject、冲突/重复提交。
- 涉及外部调用时验证 timeout、redaction、audit/metric、degraded result。
- 涉及 response shape 时加 contract 或 frontend API test。
- 行为变更后同步更新相关 `docs/` 文件。
