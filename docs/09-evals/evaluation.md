# 评测体系

**最后更新：** 2026-06-14

评测用于回答一个问题：在确定性输入和安全约束下，Agent 是否仍能给出可追踪根因、保留必要证据、拦住高风险动作并生成报告。CI 只使用 FakeLLM smoke eval；真实 LLM 或外部 provider 只能用于手动 full eval 或演示，不能作为稳定 CI 门禁。

## 模块地图

| 模块 | 职责 |
|------|------|
| `packages/evals/runner.py` | CLI 入口，运行 suite 并输出 JSON/Markdown report |
| `packages/evals/datasets/datasets.py` | 加载 `smoke` / `full` 数据集，返回 `EvalCase` |
| `packages/evals/datasets/harness.py` | 核心 harness：创建内存 DB、seed runbooks、运行 Agent、统计指标 |
| `packages/evals/harness.py` | 兼容导出，转发到 `datasets.harness` |
| `packages/evals/replay.py` | 离线重放历史 incident，与原始根因摘要对比 |
| `packages/evals/shadow.py` | shadow mode stub，只写 `EvalRun`，不触碰真实 incident/action |
| `apps/api/routers/evals.py` | Eval API：创建/list/get eval run，触发 shadow |
| `apps/worker/eval_tasks.py` | Celery task：异步运行 eval suite 并回写 `EvalRun.metrics` |

## Suite

| Suite | 用例数 | 位置 | CI 默认 | 用途 |
|-------|--------|------|---------|------|
| `smoke` | 4 | `packages/evals/datasets/smoke/*.json` | 是 | 快速验证四个原始 demo 故障：cache、DB connection、high 5xx、pod restart |
| `full` | 20 | `packages/evals/datasets/full/cases.json` | 否 | 更大样本的离线/manual eval，不作为稳定 CI 门禁 |

Smoke case IDs：

- `cache_001`
- `db_conn_001`
- `high_5xx_001`
- `pod_restart_001`

Full suite 目前是 20 个 case，覆盖 5 组 `db/high/cache/pod` 变体。

## 运行方式

CI smoke：

```bash
python -m packages.evals.runner --suite smoke --output reports/eval-smoke.json
```

本地 full：

```bash
python -m packages.evals.runner --suite full --output reports/eval-full.json
```

Runner 会同时写：

- JSON：`reports/eval-<suite>.json`
- Markdown：`reports/eval-<suite>.md`

默认输出路径可通过 `--output` 覆盖。报告包含 suite 元数据、git commit、model、prompt version、metrics 和每个 case 的详细结果。

## Harness 行为

`datasets/harness.py` 每个 case 都创建独立内存 SQLite 环境：

1. 建立 in-memory DB 并创建全部 SQLAlchemy metadata。
2. 录入 `demo/runbooks`，用于 RAG 证据。
3. 创建 incident 和 agent run。
4. 构建 `AgentDeps`，使用 fixture metrics/logs、fixture trace、fixture git、request-local tool cache、memory store、runbook retriever。
5. 使用 `InMemorySaver` 作为 LangGraph checkpointer。
6. 运行 Agent；如果进入 `waiting_approval`，最多 resume 3 次并传入 approved。
7. 持久化最终 run、report、tool calls 和 node traces。
8. 从 state、report、tool call、token/compression 数据计算 case result。

默认 settings：

| 配置 | 值 |
|------|----|
| DB | `sqlite+pysqlite:///:memory:` |
| Redis/Celery | `memory://...` |
| LLM | `LLM_PROVIDER` env，默认 `fake` |
| LLM model | `LLM_MODEL` env，默认 `fake-diagnosis-model` |
| Trace fixture | `demo/faults/traces.json` |
| Git changes fixture | `demo/faults/git_changes.json` |

真实 provider 是显式 opt-in：设置 `LLM_PROVIDER` 为非 `fake` 时，必须显式提供 `LLM_MODEL`，并按 provider 需要提供 `LLM_API_KEY`、`LLM_BASE_URL`、reasoning 和 timeout 配置。真实 provider eval 只适合手动运行。

## 指标

Suite report 的主要 metrics：

| 指标 | 含义 |
|------|------|
| `case_count` | 用例数 |
| `root_cause_top1_hit_rate` | 根因摘要命中 expected keywords 的比例 |
| `root_cause_top3_hit_rate` | hypotheses/top3 命中 expected top3 的比例 |
| `required_evidence_coverage` | 必要证据类型是否出现 |
| `high_risk_interception_rate` | expected risk 为 L2/L3 的 case 是否进入 approval interrupt |
| `json_valid_rate` | root cause、hypotheses、actions 是否是有效结构化输出 |
| `report_generation_rate` | 是否生成 incident report |
| `avg_duration_ms` | case 平均运行时长 |
| `avg_prompt_token_estimate` | 平均 prompt token 估计 |
| `avg_completion_token_estimate` | 平均 completion token 估计 |
| `p95_prompt_token_estimate` | prompt token 估计 p95 |
| `tool_success_rate` | tool call 成功比例 |
| `tool_cache_hit_rate` | tool cache 命中比例 |
| `provider_prompt_cache_hit_rate` | 当前由 harness 指标占位，不能等同真实 provider prompt cache |
| `app_prompt_segment_cache_hit_rate` | 当前由 harness 指标占位，不能等同真实 Redis app segment cache |
| `compression_retention_rate` | 压缩后 token / 压缩前 token |
| `memory_misuse_rate` | 是否错误使用不相关 memory |

CI smoke 的硬性预期来自 `tests/integration/test_eval_runner.py`：

- `case_count == 4`
- `root_cause_top1_hit_rate == 1.0`
- `root_cause_top3_hit_rate == 1.0`
- `required_evidence_coverage == 1.0`
- `high_risk_interception_rate == 1.0`
- `json_valid_rate == 1.0`
- `report_generation_rate == 1.0`
- `tool_success_rate >= 0.75`
- 每个 case 都有 `structured_output_valid=true` 和 `report_id`

## Case 结构

每个 JSON case 包含：

| 字段 | 说明 |
|------|------|
| `case_id` | 稳定用例 ID |
| `incident_type` | 人类可读故障类型 |
| `alert` | AlertCreateRequest 兼容 payload |
| `fixtures.metrics` | 指标样本，按 metric type 分组 |
| `fixtures.logs` | 日志样本，按 service label 过滤 |
| `fixtures.traces` | trace fixture path |
| `fixtures.git_changes` | deployment/git fixture path |
| `expected.root_cause_keywords` | top1 根因命中关键词 |
| `expected.top3_root_causes` | top3/root hypotheses 命中短语 |
| `expected.required_evidence_types` | 必须出现的证据类型 |
| `expected.expected_risk_level` | 用于验证高风险拦截 |

新增 case 时必须保持确定性，不使用随机时间、随机向量或真实网络。

## API 与异步任务

Eval API：

| 方法 | 路径 | 行为 |
|------|------|------|
| `POST` | `/api/evals/runs` | 创建 `EvalRun(status=queued)`，通过 Celery `run_eval_suite_task` 异步执行 suite |
| `GET` | `/api/evals/runs` | 返回最近 50 条 eval run |
| `GET` | `/api/evals/runs/{eval_run_id}` | 查询单个 eval run |
| `POST` | `/api/evals/shadow` | 触发 shadow mode stub |

`run_eval_suite_task` 会把状态改为 `running`，运行 `run_suite(suite)`，成功后写入 `status=succeeded` 和 metrics；失败时写入 `status=failed` 和错误字符串。

注意：API 触发的 eval 不会直接把 report JSON 路径作为 API 响应返回；持久化在 `EvalRun.metrics` 中。CLI 运行才写 `reports/eval-*.json` 和 `.md`。

## Replay 与 Shadow

`replay_incident()` 是离线工具：

- 从 DB 中读取历史 incident 和最新 agent run。
- 使用当前 settings 和真实 tools 重新跑 Agent。
- 将新根因摘要与原始 run state 中的根因摘要做简单相等比较。
- 返回 `EvalCaseResult`，不作为 CI 门禁。

`run_shadow_diagnosis()` 当前是安全 stub：

- 创建 `EvalRun(suite='shadow')`。
- incident 不存在时标记 `shadow_failed`。
- incident 存在时标记 `shadow_completed`，metrics 中记录模型、prompt version 和 pending note。
- 不写真实 incidents、agent_runs、approvals 或 actions。

不要把 shadow stub 解释成完整影子诊断能力；它的价值是保留 API/DB 安全形状。

## CI 与 Manual 边界

| 类型 | Provider | 允许外部调用 | 可作为 CI 稳定门禁 |
|------|----------|--------------|--------------------|
| Smoke eval | FakeLLM | 否 | 是 |
| Full eval 默认 | FakeLLM | 否 | 手动/PR 附加，不是默认 CI |
| Full eval real provider | real LLM | 是，显式 opt-in | 否 |
| Replay | 当前 settings | 可能 | 否 |
| Shadow | stub | 否 | 否 |

真实 LLM eval 结果可以用于人工比较 prompt/model 质量，但不能用于阻塞 CI。任何涉及真实 provider 的结果都必须记录 provider、model、prompt version、timeout、token 设置和运行时间。

## 添加 Eval Case Checklist

- `alert.alert_name` 能被 FakeLLM 或规则 fallback 稳定处理；未知名称会回退 high-5xx。
- `fingerprint` 唯一，避免 case 间 dedup 干扰。
- `fixtures.metrics` 覆盖 collect/verify 会查询的 metric type。
- `fixtures.logs` 的 labels 包含目标 service。
- `expected_risk_level` 与 guardrail 后预期一致，特别是 L2/L3 拦截。
- `required_evidence_types` 不应要求当前工具不会产出的类型。
- 根因关键词不要只依赖长句完全匹配；使用稳定短语。
- 新 case 跑 `python -m packages.evals.runner --suite smoke|full` 并检查 JSON/Markdown report。
