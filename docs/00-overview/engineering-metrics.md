# 工程评估指标

**最后更新：** 2026-06-17

本文定义项目级工程评估指标。目标是持续回答五个问题：主链路是否正确、风险边界是否可靠、运行是否稳定、系统是否可维护、交付是否可控。

需要沿代码路径理解 `/api/evals/engineering-metrics` 如何读取最新成功 smoke eval、业务运行时表、外部 unknown 指标和 scorecard 时，见 [测试、Eval 与工程指标技术深挖](testing-eval-engineering-metrics-deep-dive.md)。

## 指标入口

| 入口 | 用途 |
|------|------|
| `GET /api/evals/engineering-metrics?window_days=30` | 从业务库和最新 smoke eval 聚合只读工程指标。 |
| `GET /metrics` | Prometheus text endpoint，暴露运行时 counters/histograms/gauges。 |
| `python -m packages.evals.runner --suite smoke --output reports/eval-smoke.json` | 生成确定性 FakeLLM smoke eval 报告。 |
| CI coverage / VCS / CD 系统 | 覆盖率、CI 状态、DORA 指标等外部来源。 |

`engineering-metrics` endpoint 不调用外部 CI、VCS、Prometheus 或云服务，也不产生数据库写入。无法从业务库计算的指标以 `status=unknown` 返回，并在 `source` 字段标明应由哪个外部系统提供。未知外部指标不伪造分数，计入 `completeness_rate`。

响应中的每个指标包含：

```json
{
  "key": "high_risk_interception_rate",
  "category": "safety",
  "label": "High-risk interception rate",
  "value": 1.0,
  "unit": "ratio",
  "target": "1.0",
  "status": "pass",
  "score": 100.0,
  "weight": 1.0,
  "source": "latest_smoke_eval",
  "description": "...",
  "reproduction": [
    "python -m packages.evals.runner --suite smoke --output reports/eval-smoke.json"
  ]
}
```

`status` 取值为 `pass`、`fail`、`warn`、`unknown`。`warn` 表示指标可计算但不一定是硬门禁，例如缓存命中率或显式 opt-in 的 live executor 使用记录。每个可计算指标都有 0-100 分；未知指标 `score=null`。

响应还包含 `scorecard`：

```json
{
  "overall_score": 93.8,
  "gate_status": "warn",
  "completeness_rate": 0.5517,
  "metric_count": 58,
  "scored_metric_count": 32,
  "unknown_metric_count": 26,
  "category_scores": [],
  "top_risks": [],
  "reproduction": []
}
```

## 评分模型

分类权重：

| 类别 | 权重 |
|------|------|
| Safety | 25% |
| Quality | 20% |
| Reliability | 20% |
| Performance | 10% |
| Maintainability | 10% |
| Delivery | 10% |
| Efficiency | 5% |

单项评分规则：

| 规则 | 公式 |
|------|------|
| 最小阈值，如 `>= 0.95` | `min(100, value / target * 100)` |
| 最大阈值，如 `<= 5000 ms` | 达标为 `100`，超标为 `target / value * 100` |
| 零违规数，如 L4 未拦截数 | `0` 时 `100`，非零硬门禁为 `0` |
| 明确 opt-in 警告，如 non-fixture executor | `0` 时 `100`，非零为 `70` 并标记 `warn` |
| 未知外部指标 | `score=null`，不进入加权总分，计入完整度 |

`overall_score` 是已知分类分按分类权重加权后的结果。`completeness_rate` 表示已评分指标占全部指标的比例。`gate_status` 规则：

- `fail`：硬门禁失败，或整体分 `< 80`。
- `warn`：存在非硬门禁失败/警告/未知指标，或整体分 `< 95`，或完整度 `< 1.0`。
- `pass`：无失败、无警告、无未知，且整体分 `>= 95`。

## 核心指标

| 类别 | 指标 key | 目标 | 来源 |
|------|----------|------|------|
| 质量 | `root_cause_top1_hit_rate` | smoke CI gate 为 `1.0` | 最新成功 smoke eval |
| 质量 | `root_cause_top3_hit_rate` | smoke CI gate 为 `1.0` | 最新成功 smoke eval |
| 质量 | `required_evidence_coverage` | smoke CI gate 为 `1.0` | 最新成功 smoke eval |
| 质量 | `json_valid_rate` | `1.0` | 最新成功 smoke eval |
| 质量 | `evidence_traceability_rate` | `>= 0.95` | `agent_runs` + `evidence_items` |
| 质量 | `evidence_record_completeness_rate` | `>= 0.95` | `agent_runs` + `evidence_items` |
| 质量 | `report_section_completeness_rate` | `>= 0.95` | `incident_reports` |
| 质量 | `report_version_issue_count` | `0` | `incident_reports` |
| 安全 | `high_risk_interception_rate` | `1.0` | 最新成功 smoke eval |
| 安全 | `unapproved_high_risk_execution_count` | `0` | `actions` + `approvals` |
| 安全 | `l3_approval_missing_confirmation_count` | `0` | `actions` + `approvals` |
| 安全 | `l4_approval_count` | `0` | `actions` + `approvals` |
| 安全 | `l4_not_blocked_count` | `0` | `actions` |
| 安全 | `waiting_approval_backlog_count` | local/CI demo 为 `0` | `approvals` |
| 安全 | `approval_decision_rate` | `>= 0.95` | `approvals` |
| 安全 | `l2_l3_approval_coverage_rate` | `1.0` | `actions` + `approvals` |
| 安全 | `l3_confirmation_valid_rate` | `1.0` | `actions` + `approvals` |
| 可靠性 | `active_incident_backlog_count` | local/CI demo 为 `0` | `incidents` |
| 可靠性 | `open_incident_count` | local/CI demo 为 `0` | `incidents` |
| 可靠性 | `incident_resolution_rate` | `>= 0.95` | `incidents` |
| 可靠性 | `agent_run_success_rate` | `>= 0.95` | `agent_runs` |
| 可靠性 | `runtime_report_generation_rate` | `>= 0.95` | `agent_runs` + `incident_reports` |
| 可靠性 | `runtime_tool_success_rate` | `>= 0.90` | `tool_calls` |
| 可靠性 | `runtime_tool_degraded_rate` | `<= 0.10` | `tool_calls` |
| 可靠性 | `tool_call_coverage_rate` | `>= 0.95` | `agent_runs` + `tool_calls` |
| 可靠性 | `executed_action_success_rate` | `>= 0.95` | `actions` |
| 可靠性 | `agent_node_success_rate` | `>= 0.95` | `agent_run_nodes` |
| 可靠性 | `failed_agent_node_count` | `0` | `agent_run_nodes` |
| 可靠性 | `checkpoint_pointer_coverage_rate` | `>= 0.95` | `agent_runs` |
| 可靠性 | `agent_node_trace_coverage_rate` | `>= 0.95` | `agent_runs` + `agent_run_nodes` |
| 性能 | `diagnosis_duration_p95_ms` | `<= 60000` | `agent_runs.duration_ms` |
| 性能 | `tool_call_duration_p95_ms` | `<= 5000` | `tool_calls.duration_ms` |
| 性能 | `agent_node_duration_p95_ms` | `<= 10000` | `agent_run_nodes.duration_ms` |
| 性能 | `eval_avg_duration_ms` | smoke eval `<= 15000` | 最新成功 smoke eval |
| 效率 | `provider_prompt_cache_hit_rate` | `>= 0.60`，不可用时 `unknown` | `agent_runs` |
| 效率 | `app_prompt_segment_cache_hit_rate` | `>= 0.70` | `agent_runs` |
| 效率 | `runtime_tool_cache_hit_rate` | 跟踪指标，低于 `0.50` 为 `warn` | `tool_calls` |
| 效率 | `eval_p95_prompt_token_estimate` | smoke eval `<= 3000` | 最新成功 smoke eval |
| 效率 | `eval_tool_cache_hit_rate` | smoke eval `>= 0.10` | 最新成功 smoke eval |
| 效率 | `eval_compression_retention_rate` | `<= 1.0` | 最新成功 smoke eval |

## 外部指标

这些指标是项目评价的一部分，但不由业务数据库直接提供：

| 类别 | 指标 key | 目标 | 期望来源 |
|------|----------|------|----------|
| 可维护性 | `backend_test_coverage` | 后端总覆盖率 `> 80%`，核心包关注 `>= 85%` | pytest-cov / CI |
| 安全 | `guardrail_test_coverage` | `>= 95%` | pytest-cov / CI |
| 可维护性 | `frontend_test_coverage` | statements/branches/functions/lines `> 80%` | Vitest coverage |
| 交付 | `ci_pipeline_status` | passing | CI provider |
| 可维护性 | `ruff_lint_status` | passing | ruff / CI |
| 可维护性 | `mypy_type_check_status` | passing | mypy / CI |
| 安全 | `dependency_vulnerability_status` | 无 high/critical 漏洞 | pip/npm audit |
| 质量 | `api_contract_test_status` | passing | contract tests |
| 性能 | `api_latency_p95_ms` | alert ingest `<= 300 ms`，读接口 `<= 200 ms` | Prometheus HTTP metrics |
| 交付 | `dora_deployment_frequency` | 团队定义 | VCS/CD |
| 交付 | `dora_lead_time_for_changes` | 团队定义 | VCS/CD |
| 交付 | `dora_change_failure_rate` | 团队定义 | VCS/CD + incident records |
| 交付 | `dora_mttr` | 团队定义 | production incident lifecycle |

## 使用建议

- 本地和 CI 先看 smoke eval、guardrail、L2/L3/L4、fixture executor 和 report generation 指标。
- 生产或准生产环境再接入 Prometheus、CI、VCS/CD，把 `unknown` 指标补齐。
- `provider_prompt_cache_hit_rate` 与 `app_prompt_segment_cache_hit_rate` 必须分开解释；provider 不返回缓存数据时应保持 `unknown`。
- `non_fixture_executor_action_count` 在 local/CI 应为 `0`；生产中非零只表示 live executor 曾被显式使用，需要结合配置和审计判断。

## 复现命令

本地复现工程评分：

```bash
docker compose up -d postgres redis prometheus loki api
curl http://localhost:8000/healthz
curl http://localhost:8000/readyz
curl http://localhost:8000/api/evals/engineering-metrics?window_days=30
```

补齐 smoke eval 指标：

```bash
python -m packages.evals.runner --suite smoke --output reports/eval-smoke.json
```

补齐后端覆盖率指标：

```bash
pytest tests/unit tests/integration --cov=apps --cov=packages --cov-report=term-missing --cov-report=xml --cov-fail-under=80
pytest tests/contract
ruff check apps packages tests
mypy apps packages
```

补齐前端覆盖率和浏览器 smoke：

```bash
cd apps/web
npm run test:coverage
npm run test:e2e
npm audit --audit-level=high
```

补齐 Python 依赖漏洞指标：

```bash
python -m pip-audit
```
