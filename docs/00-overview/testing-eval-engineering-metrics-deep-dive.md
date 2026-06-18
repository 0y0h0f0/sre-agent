# 测试、Eval 与工程指标技术深挖

**最后更新：** 2026-06-17

本文沿当前代码路径说明测试门禁、Eval harness 和工程指标如何共同证明系统质量。它补充 [测试策略](../07-testing/testing-strategy.md)、[评测体系](../09-evals/evaluation.md) 和 [工程评估指标](engineering-metrics.md)：这些文档分别列出策略、suite 和指标定义；本文解释 CI、pytest fixture、Vitest/Playwright、FakeLLM smoke eval、Eval API/Celery task 和 `/api/evals/engineering-metrics` 如何组合。

## 阅读目标

读完本文应能回答：

- CI 实际运行哪些后端、前端和 Eval 门禁，哪些测试不在默认 CI。
- `tests/conftest.py` 如何隔离本地环境变量、外部代理、Kube 配置和 API key auth。
- pytest、coverage、ruff、mypy、Vitest、Playwright 的配置入口在哪里。
- Smoke/full eval case 如何加载，harness 如何用内存 DB、fixture tool、FakeEmbedding 和 `InMemorySaver` 运行 Agent。
- Eval API、`run_eval_suite_task`、CLI `packages.evals.runner` 的行为差异是什么。
- 工程指标 endpoint 从哪些业务表读取数据，哪些指标来自最新成功 smoke eval，哪些指标故意返回 `unknown`。
- `provider_prompt_cache_hit_rate`、`app_prompt_segment_cache_hit_rate`、`tool_cache_hit_rate` 在测试/Eval/运行时指标中的边界。
- 一个失败的 CI、Eval 或工程指标应该先看哪段代码和哪类记录。

## 代码入口

| 主题 | 当前入口 |
|------|----------|
| CI workflow | `.github/workflows/ci.yml` |
| Python test config | `pyproject.toml` |
| Python fixture defaults | `tests/conftest.py` |
| Backend unit/integration tests | `tests/unit/`、`tests/integration/` |
| Python E2E/contract/manual tests | `tests/e2e/`、`tests/contract/`、`tests/manual/` |
| Frontend test scripts | `apps/web/package.json` |
| Vitest coverage config | `apps/web/vitest.config.ts` |
| Playwright config | `apps/web/playwright.config.ts` |
| Eval CLI | `packages/evals/runner.py` |
| Eval datasets | `packages/evals/datasets/smoke/`、`packages/evals/datasets/full/cases.json` |
| Eval harness | `packages/evals/datasets/harness.py` |
| Eval API router/service | `apps/api/routers/evals.py`、`apps/api/services/eval_service.py` |
| Eval Celery task | `apps/worker/eval_tasks.py` |
| Shadow/replay helpers | `packages/evals/shadow.py`、`packages/evals/replay.py` |
| Engineering metrics service | `apps/api/services/engineering_metrics_service.py` |
| Engineering metrics repository | `packages/db/repositories/engineering_metrics.py` |
| Engineering metrics tests | `tests/integration/test_engineering_metrics_api.py` |
| Smoke eval tests | `tests/integration/test_eval_runner.py` |

## 总链路

```text
code change
  -> targeted unit/integration/frontend tests
  -> CI backend job
       -> ruff
       -> mypy
       -> pytest unit+integration with coverage
       -> FakeLLM smoke eval CLI
  -> CI frontend job
       -> Vitest coverage
       -> TypeScript/Vite build
       -> Playwright browser smoke
  -> optional API/Celery eval run
       -> EvalRun(status=queued/running/succeeded/failed)
       -> metrics stored in eval_runs.metrics
  -> engineering metrics endpoint
       -> latest succeeded smoke eval metrics
       -> runtime DB metrics from incidents/runs/tools/actions/approvals/reports
       -> external CI/DORA/latency metrics as unknown placeholders
```

`AGENTS.md` 对 Codex 的执行策略更严格：Codex 不直接运行 `pytest`、前端测试、Playwright 或完整测试套件。需要验证时，Codex 应提供命令，由用户本地运行并回贴结果。本文保留命令，是为了开发者和 CI 复现。

## 1. CI Gate

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

默认 CI 不运行：

- `tests/e2e/` 里的 Python TestClient 端到端测试。
- `tests/contract/`。
- `tests/manual/`。
- `packages.evals.runner --suite full`。
- 真实 LLM、真实 SMTP、真实 Kubernetes 或 cloud provider 测试。

这意味着“CI 通过”证明的是确定性 local/CI 路径：FakeLLM、fixture/mock 后端、fixture executor、离线 smoke eval 和前端浏览器 smoke。生产后端对接、M9 外部调用、真实 LLM 质量只能作为手动验证或 rollout 附加证据，不能变成稳定 CI gate。

## 2. Python Test Topology

当前 Python 测试文件：

| 层级 | 文件数 | 说明 |
|------|--------|------|
| `tests/unit/` | 77 | 纯函数、schema、settings、guardrail、工具、RAG、memory、discovery、M9 安全 |
| `tests/integration/` | 27 | API、repository、Celery task、worker audit、approval、report、eval、engineering metrics |
| `tests/e2e/` | 4 | TestClient 级主链路和 M9 专项 E2E 结构测试 |
| `tests/contract/` | 1 | API response shape 契约 |
| `tests/manual/` | 2 | 真实 SMTP 手动连通性和发送测试 |

`pyproject.toml` 的 pytest 配置：

```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-ra"
pythonpath = ["."]
markers = [
  "real_email: sends a real email through configured SMTP; skipped unless RUN_REAL_EMAIL_TEST=true",
]
```

`tests/conftest.py` 的 autouse fixture `_isolate_settings_in_tests()` 做了这些事情：

- 禁用 `Settings` 的 `.env` 文件读取。
- 删除所有 `Settings.model_fields` 对应的大小写环境变量。
- 删除代理和 Kube 相关外部变量：`ALL_PROXY`、`HTTP_PROXY`、`HTTPS_PROXY`、`NO_PROXY`、`KUBECONFIG` 等。
- 设置 `API_KEY_AUTH_ENABLED=false`。
- 每次测试前后清理 `get_settings()` cache。

常用 fixture：

| Fixture | 行为 |
|---------|------|
| `db_session` | SQLite in-memory + `StaticPool`，每个测试创建/销毁 metadata。 |
| `test_settings` | SQLite + memory Redis/Celery + eager task + fake LLM/embedding。 |
| `client` | FastAPI TestClient，覆盖 DB、诊断入队、resume 入队、通知入队和 settings。 |
| `fake_enqueue` | 只记录 `(incident_id, agent_run_id)`，返回假 task ID。 |
| `fake_resume_enqueue` | 只记录 `(agent_run_id, decision)`。 |
| `fake_notification_enqueue` | 只记录邮件通知入队 payload。 |

测试默认隔离本机代理和 Kube 配置，是为了避免开发机环境把离线测试变成真实网络调用。

## 3. Coverage, Ruff, Mypy

`pyproject.toml` 的静态和覆盖率配置：

| 工具 | 当前配置 |
|------|----------|
| Ruff | `line-length=100`，`target-version=py311`，select `E/F/I/B/UP`，ignore `B008`。 |
| Mypy | `strict=true`，`ignore_missing_imports=true`，`warn_unused_ignores=false`。 |
| Coverage | branch coverage，source 为 `apps` 和 `packages`。 |
| Coverage omit | `migrations/*`、`tests/*`、`packages/evals/*`。 |

`packages/evals/*` 不计入 pytest-cov 分母。因此 Eval runner/harness 的行为主要通过 `tests/integration/test_eval_runner.py` 和 CLI smoke eval 保证，而不是通过 coverage 百分比保证。

后端硬门禁是总体 `--cov-fail-under=80`。项目质量目标仍是核心包更高覆盖：`packages/agent`、`packages/tools`、`packages/rag`、`packages/db` 关注 85%+，`packages/agent/guardrails` 关注 95%+。

## 4. Frontend Test Gate

`apps/web/package.json`：

```json
{
  "test": "vitest run",
  "test:coverage": "vitest run --coverage",
  "test:e2e": "playwright test"
}
```

当前前端测试：

| 文件 | 当前测试数 | 主要覆盖 |
|------|------------|----------|
| `apps/web/src/App.test.tsx` | 20 | 页面渲染、API key、incident/detail/run/report/approval、WebSocket 更新、通知、L3 二次确认、错误和空状态。 |
| `apps/web/src/api.test.ts` | 11 | API key header、request id、标准错误信封、approval mutation、API helper endpoint。 |
| `apps/web/src/e2e/smoke.spec.ts` | 1 | 浏览器内主 smoke：创建 alert、查看 incident/report、审批 L3。 |

Vitest 配置：

- `environment=jsdom`。
- include `src/**/*.{test,spec}.{ts,tsx}`。
- exclude `src/e2e/**`、`node_modules/**`、`dist/**`。
- coverage include `src/**/*.{ts,tsx}`。
- coverage exclude `src/main.tsx`、`src/e2e/**`、`src/test/**`、test files。
- statements/branches/functions/lines 阈值均为 80。

Playwright 配置：

- `testDir=./src/e2e`。
- 自动启动 `npm run dev -- --port 5173`。
- `baseURL=http://127.0.0.1:5173`。
- `reuseExistingServer=true`。

Playwright smoke 使用 route mock，不依赖真实 API 服务；它验证前端主交互能在浏览器中连贯运行。

## 5. Eval Suite and Harness

Eval suite 入口：

| Suite | 当前用例 | 来源 |
|-------|----------|------|
| `smoke` | 4 | `packages/evals/datasets/smoke/*.json` |
| `full` | 20 | `packages/evals/datasets/full/cases.json` |

Smoke case：

- `cache_001`
- `db_conn_001`
- `high_5xx_001`
- `pod_restart_001`

CLI 入口 `packages/evals/runner.py` 只做三件事：

```text
parse --suite/--output
-> run_suite(suite, output)
-> print markdown report and output path
```

`run_suite()` 的路径：

```text
load_suite_cases(suite)
-> settings = _eval_settings()
-> run_case(case) for each case
-> _suite_metrics(results)
-> write JSON report
-> write Markdown report
```

默认输出：

- JSON：`reports/eval-<suite>.json`
- Markdown：`reports/eval-<suite>.md`

`run_case()` 为每个 case 创建独立环境：

```text
_eval_settings()
-> _make_environment(): in-memory SQLite + StaticPool
-> create all SQLAlchemy metadata
-> seed demo/runbooks
-> create Incident + AgentRun
-> _build_deps()
-> checkpointer = InMemorySaver()
-> AgentRunner.run()
-> if waiting_approval: approve waiting approvals in DB and resume, up to 6 loops
-> _finalize_run()
-> collect tool calls, report, state metrics
-> return EvalCaseResult
```

关键确定性边界：

- DB 固定为 `sqlite+pysqlite:///:memory:`。
- Redis/Celery URL 固定为 `memory://...`。
- embedding 固定为 `fake`。
- reranker 固定为 `fake`。
- trace/git fixture 使用 `demo/faults/traces.json` 和 `demo/faults/git_changes.json`。
- runbook ingest/search 显式注入 `FakeEmbeddingProvider` 和 `FakeRerankerBackend`。
- SQLite 下 hybrid search 走词法 fallback，不执行 PostgreSQL 专用 full-text SQL。
- `InMemorySaver` 用于 eval checkpoint，生产 worker 仍使用 PostgreSQL `PostgresSaver`。

LLM provider：

- 默认 `LLM_PROVIDER=fake`，`LLM_MODEL=fake-diagnosis-model`。
- 如果 `LLM_PROVIDER` 不是 `fake`，必须显式设置 `LLM_MODEL`。
- 真实 provider eval 可读取 `LLM_API_KEY`、`LLM_BASE_URL`、`LLM_REASONING_ENABLED`、`LLM_REASONING_EFFORT`、`LLM_REASONING_NODES`、`LLM_MAX_TOKENS`、`LLM_TIMEOUT_SECONDS`。
- 真实 provider eval 只能手动运行，不能作为稳定 CI gate。

审批恢复细节：

- Harness 直接调用 `AgentRunner`，没有通过 API approval service。
- 因此 `_approve_waiting_approvals()` 会模拟 API 的副作用：把 waiting approval 更新为 approved，并同步 action status。
- 对 L3 action，会先写 `risk_ack=true`、`confirm_action_type=action.type`、`confirm_target=action.target`。
- 这保证 eval 不绕过 L3 二次确认约束。

## 6. Eval Metrics

`_suite_metrics()` 输出的核心指标：

| 指标 | 来源 |
|------|------|
| `case_count` | case 数量 |
| `root_cause_top1_hit_rate` | root cause summary 是否包含 expected keywords |
| `root_cause_top3_hit_rate` | root cause/hypotheses 是否命中 expected top3 |
| `required_evidence_coverage` | state 中是否出现 required evidence types |
| `high_risk_interception_rate` | expected L2/L3 case 是否进入 approval interrupt |
| `json_valid_rate` | root cause、hypotheses、recommended actions 是否结构化 |
| `report_generation_rate` | 是否生成 incident report |
| `avg_duration_ms` | case 平均运行时长 |
| `p95_prompt_token_estimate` | prompt token 估计 p95 |
| `tool_success_rate` | persisted tool call 成功比例 |
| `tool_cache_hit_rate` | persisted tool call cache hit 比例 |
| `compression_retention_rate` | 压缩后 token / 压缩前 token |
| `memory_misuse_rate` | 是否使用了不相关 memory |

当前 `_suite_metrics()` 里的 `provider_prompt_cache_hit_rate` 和 `app_prompt_segment_cache_hit_rate` 是 harness 占位值，按 `tool_cache_hits / tool_total` 计算。它们不能解释成真实 provider prompt cache 或 Redis app prompt segment cache。

运行时工程指标中的 `provider_prompt_cache_hit_rate` 和 `app_prompt_segment_cache_hit_rate` 则来自 `agent_runs.provider_cache_*` 和 `agent_runs.app_cache_*` 计数。两者来源不同，排障时不能混用。

`tests/integration/test_eval_runner.py` 当前固定验证：

- smoke suite 有 4 个 case。
- expected risk 覆盖 `L1/L2/L3`。
- smoke report 写出 JSON 和 Markdown。
- Top1、Top3、required evidence、high-risk interception、JSON validity、report generation 都为 1.0。
- `tool_success_rate >= 0.75`。
- 每个 case 都有 `structured_output_valid=true` 和 `report_id`。
- harness 强制使用 fake embedding/reranker，即使环境变量设置了 `bge_zh`、`bge` 或代理。

## 7. Eval API and Celery Task

Eval HTTP API：

| 方法 | 路径 | 当前行为 |
|------|------|----------|
| `POST` | `/api/evals/runs` | 创建 `EvalRun(status=queued)`，提交 DB，然后异步入队 `run_eval_suite_task`。 |
| `GET` | `/api/evals/runs` | 返回最近 50 条 eval run。 |
| `GET` | `/api/evals/runs/{eval_run_id}` | 返回单条 eval run。 |
| `POST` | `/api/evals/replay` | 创建 `EvalRun(suite='replay')`，提交 DB，然后异步入队 `run_replay_eval_task`。 |
| `POST` | `/api/evals/shadow` | 调用 shadow stub。 |
| `GET` | `/api/evals/engineering-metrics` | 聚合工程指标。 |

`EvalRunRequest`：

```text
suite: "smoke" | "full"  # Pydantic pattern validates
model: optional string
prompt_version: default "v1"
```

`EvalService.trigger_smoke_eval()` 当前流程：

```text
create EvalRun(status=queued, suite, model_name, prompt_version)
-> flush + commit
-> run_eval_suite_task.delay(eval_run_id, suite, model_name, prompt_version)
-> if enqueue fails: status=enqueue_failed and commit
-> return eval_run_id/status/created_at
```

`run_eval_suite_task()` 当前流程：

```text
load EvalRun by eval_run_id
-> if missing: return error
-> status=running, started_at=utc_now(), commit
-> report = run_suite(suite)
-> status=succeeded, metrics=report.metrics, finished_at=utc_now(), commit
-> on exception: status=failed, metrics={"error": str(exc)}, finished_at=utc_now(), commit
```

当前细节：

- Task 装饰器设置 `max_retries=2`，但函数内部捕获异常并标记 failed 返回；它不会对普通 harness 异常自动 retry。
- `model` 和 `prompt_version` 会记录在 `EvalRun`，但 task 调用的是 `run_suite(suite)`；实际 LLM provider/model 仍由 `_eval_settings()` 从环境变量决定。
- `run_suite()` 生成本地 report 文件；API 响应不会返回 report 文件路径，API 消费者应读取 `EvalRun.metrics`。

## 8. Replay and Shadow

`packages/evals/replay.py` 提供单 incident replay 和 batch replay：

- 从源 DB 读取历史 incident 和最新 agent run；源库只读。
- 每个 replay case 在独立 SQLite 临时 DB 中克隆 incident、agent run 和 runbook chunks。
- 强制 fixture executor、Fake embedding、Fake reranker；L2/L3 用内存 checkpointer 停在审批处。
- 比较新 root cause summary 与原始 run state 中的 summary，batch metrics 写入 `EvalRun.metrics`。
- 不作为 CI 门禁；当前 settings 的真实只读工具可能被调用。

`packages/evals/shadow.py:run_shadow_diagnosis()` 当前是安全 stub：

- 只创建 `EvalRun(suite="shadow")`。
- incident 不存在时写 `shadow_failed`。
- incident 存在时写 `shadow_completed` 和 pending note。
- 不写真实 incident、agent_run、approval 或 action。

不要把 shadow stub 解释成完整影子诊断能力。当前价值是保留 API/DB 形状，同时不产生诊断副作用。

## 9. Engineering Metrics Endpoint

`GET /api/evals/engineering-metrics?window_days=30` 是只读 endpoint。

`EngineeringMetricsService.get_summary()`：

```text
generated_at = utc_now()
window_started_at = generated_at - window_days
-> list incidents/runs/tool_calls/nodes/actions/approvals/reports/evidence
-> latest succeeded EvalRun where suite=smoke and created_at >= window_started_at
-> build eval metrics
-> build runtime quality metrics
-> build incident backlog metrics
-> build safety metrics
-> build tool/performance metrics
-> build workflow integrity metrics
-> append unknown external metrics
-> build scorecard
```

Repository 读取范围：

| 数据 | 查询时间字段 |
|------|--------------|
| `AgentRun` | `created_at >= since` |
| `Incident` | `created_at >= since` |
| `ToolCall` | `created_at >= since` |
| `AgentRunNode` | `created_at >= since` |
| `Action` | `created_at >= since` |
| `Approval` | `requested_at >= since` |
| `IncidentReport` | `created_at >= since` |
| `EvidenceItem` | `created_at >= since` |
| latest smoke eval | `suite="smoke"`、`status="succeeded"`、`created_at >= since` |

响应结构：

- `summary`：按 agent runs、incidents、tool calls、actions、approvals、latest smoke eval 汇总数量。
- `metrics`：逐项指标，含 key/category/value/status/score/source/reproduction。
- `scorecard`：overall score、gate status、completeness、分类分、top risks、复现命令。

## 10. Engineering Metric Categories

当前分类权重：

| 类别 | 权重 |
|------|------|
| safety | 0.25 |
| quality | 0.20 |
| reliability | 0.20 |
| performance | 0.10 |
| maintainability | 0.10 |
| delivery | 0.10 |
| efficiency | 0.05 |

硬门禁 key：

- `high_risk_interception_rate`
- `unapproved_high_risk_execution_count`
- `l3_approval_missing_confirmation_count`
- `l4_approval_count`
- `l4_not_blocked_count`

只要硬门禁指标状态为 `fail`，`scorecard.gate_status` 就是 `fail`。普通指标失败会拉低分类分和整体分；warn/unknown 会让最终状态保持 `warn`，除非整体分或硬门禁触发 fail。

`unknown` 外部指标：

| key | source |
|-----|--------|
| `backend_test_coverage` | `ci_coverage` |
| `guardrail_test_coverage` | `ci_coverage` |
| `frontend_test_coverage` | `frontend_ci` |
| `ci_pipeline_status` | `ci` |
| `ruff_lint_status` | `static_analysis` |
| `mypy_type_check_status` | `static_analysis` |
| `dependency_vulnerability_status` | `security_scan` |
| `api_contract_test_status` | `contract_tests` |
| `api_latency_p95_ms` | `prometheus` |
| `dora_deployment_frequency` | `vcs_ci_cd` |
| `dora_lead_time_for_changes` | `vcs_ci_cd` |
| `dora_change_failure_rate` | `vcs_ci_cd` |
| `dora_mttr` | `incident_process` |

这些指标不从业务数据库计算，endpoint 也不会调用 CI、VCS、Prometheus 或云服务。未知指标 `score=null`，不进入加权总分，但计入 `completeness_rate`。

`non_fixture_executor_action_count` 是 warn 指标，不是硬 fail。local/CI 应为 0；生产非零只能说明 live executor 曾被显式使用，需要结合配置和审计判断。

## 11. Debugging Paths

| 现象 | 首看 | 判断方向 |
|------|------|----------|
| pytest coverage 失败 | pytest output、coverage missing lines | 行为测试缺失，或代码分支没有覆盖；Eval 包不计入 coverage。 |
| ruff 失败 | ruff output | import 顺序、未使用变量、bugbear、pyupgrade。 |
| mypy 失败 | mypy output | strict type、Optional、Pydantic/SQLAlchemy typing。 |
| smoke eval root cause 失败 | `reports/eval-smoke.json` case detail、`packages/evals/datasets/smoke/*.json` | FakeLLM/rules fallback、expected keywords、runbook fixture、evidence fixture。 |
| smoke eval high-risk interception 失败 | case `expected_risk_level`、guardrail policy、approval node | L2/L3 是否进入 interrupt，是否被自动执行。 |
| smoke eval evidence coverage 失败 | case `expected.required_evidence_types`、state evidence keys | tool fixture 是否产出目标 evidence type，runbook 是否被检索。 |
| API eval run 卡住 queued | `eval_runs.status`、Celery worker logs | `run_eval_suite_task.delay()` 是否成功，worker 是否在线。 |
| API eval run failed | `eval_runs.metrics.error` | harness 异常、suite 名称、真实 LLM env 缺失。 |
| engineering metrics 全是 unknown | 是否有成功 smoke `EvalRun` 和运行时业务记录 | 空 DB 只会有部分零违规指标可评分，外部指标保持 unknown。 |
| engineering hard gate fail | 对应 action/approval/eval metric | 优先排查 L2/L3 未审批执行、L3 确认字段、L4 是否被审批或未 block。 |
| cache 指标看起来矛盾 | 指标 source 和 key | Eval provider/app cache 是 harness 占位；运行时 provider/app cache 来自 `agent_runs` 计数；tool cache 来自 `tool_calls`。 |
| Playwright smoke 失败 | `apps/web/src/e2e/smoke.spec.ts`、trace/screenshot | 前端路由、可访问性 label、mocked API shape、Vite dev server。 |

## 12. Change Checklist

新增或修改测试能力：

- 先确认变更属于 unit、integration、contract、E2E、manual 还是 Eval。
- 不依赖真实网络、随机时间、随机向量或本机代理。
- 涉及时间时使用固定 UTC 时间或 monkeypatch。
- 涉及外部调用时覆盖 timeout、redaction、audit/metric 和 degraded result。
- 涉及风险等级时覆盖正向和负向路径，特别是 L3 缺字段和 L4 direct reject。
- 涉及 API response shape 时同步 frontend API test 或 contract test。

新增 Eval case：

- `case_id` 稳定，`fingerprint` 唯一。
- `alert.alert_name` 能被 FakeLLM 或规则 fallback 稳定处理。
- metrics/logs/traces/git fixtures 覆盖 collect 和 verify 需要的数据。
- `expected_risk_level` 与 guardrail 后实际行为一致。
- `required_evidence_types` 不要求当前工具不会产出的类型。
- root cause keywords 使用稳定短语，不依赖完整长句匹配。

新增工程指标：

- 先判断 source 是 `database`、`latest_smoke_eval` 还是外部系统。
- 外部系统指标保持 `unknown`，不要在 API 内调用 CI/VCS/Prometheus 或伪造分数。
- 安全硬门禁只加入真正不能放宽的边界。
- 每个指标必须有 reproduction 命令或来源说明。
- 更新 `tests/integration/test_engineering_metrics_api.py`。

## 13. Verification Commands

开发者本地复现后端 CI：

```bash
ruff check apps packages tests
mypy apps packages
pytest tests/unit tests/integration --cov=apps --cov=packages --cov-report=term-missing --cov-fail-under=80
python -m packages.evals.runner --suite smoke --output reports/eval-smoke.json
```

开发者本地复现前端 CI：

```bash
cd apps/web
npm run test:coverage
npm run build
npm run test:e2e
```

附加验证：

```bash
pytest tests/contract/test_runbook_api_contract.py -v
pytest tests/e2e/test_e2e_flows.py -v
python -m packages.evals.runner --suite full --output reports/eval-full.json
curl http://localhost:8000/api/evals/engineering-metrics?window_days=30
```

Codex 在本项目内不直接执行这些测试命令；需要用户本地运行并反馈结果。
