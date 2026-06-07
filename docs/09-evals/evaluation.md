# 评测体系

## 目标

评测用于验证 Agent 在本地 deterministic 条件下是否满足：

- 根因命中。
- top-3 hypothesis 命中。
- 必要证据命中。
- 高风险动作阻断。
- JSON/结构化输出有效。
- memory/cache/compression 指标可解释。
- 报告生成。

## 数据集

Smoke 数据集：

```text
packages/evals/datasets/smoke/
  cache_001.json
  db_conn_001.json
  high_5xx_001.json
  pod_restart_001.json
```

Full 数据集：

```text
packages/evals/datasets/full/cases.json
```

Smoke eval 必须至少覆盖 MVP 四类事故。

## 命令

```bash
python -m packages.evals.runner --suite smoke
python -m packages.evals.runner --suite full --output reports/eval-full.json
```

默认输出：

```text
reports/eval-smoke.json
reports/eval-full.json
```

## FakeLLM 要求

CI smoke eval 必须使用 FakeLLM。真实 LLM 可以用于 manual full eval，但不作为稳定 CI gate。

## Harness 行为

Eval harness 会：

1. 创建内存/SQLite 风格测试环境。
2. 初始化 schema。
3. 入库 demo Runbook。
4. 创建 incident 和 agent run。
5. 构造 fixture tools。
6. 使用 `InMemorySaver` checkpointer。
7. 运行 Agent。
8. 如发生 approval interrupt，最多 resume 3 次。
9. 计算指标。
10. 输出 JSON 和 Markdown 报告。

## 指标字段

每个 case 记录：

- `case_id`
- `incident_type`
- `status`
- `approval_interrupted`
- `root_cause_summary`
- `root_cause_hit`
- `top3_hit`
- `required_evidence_hit`
- `expected_risk_level`
- `actual_risk_level`
- `duration_ms`
- `tool_total`
- `tool_successes`
- `tool_cache_hits`
- `prompt_token_estimate`
- `completion_token_estimate`
- `compression_retention_rate`
- `structured_output_valid`
- `memory_misuse`
- `report_id`
- `report_version`

Suite metrics 汇总到 `EvalSuiteReport.metrics`。

## CI 门禁建议

Smoke eval 应强制：

- high-risk action block rate = 100%。
- JSON output validity = 100%。
- FakeLLM。
- 不依赖外部 LLM provider。
- 不执行真实动作。

## Shadow mode

`packages/evals/shadow.py` 当前提供 side-effect-free shadow eval 记录路径：

- 只写 `eval_runs`。
- 不修改真实 incidents、agent_runs、approvals、actions。
- 用于记录 shadow model/prompt 元信息。

当前完成口径是记录 shadow model/prompt 元信息并只写 eval 表；若需要实际替代模型并行诊断，应作为生产部署扩展单独开启，仍不得执行真实动作。
