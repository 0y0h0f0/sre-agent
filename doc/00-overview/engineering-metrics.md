# 工程指标

指标必须在 README、CI 输出和评测报告中可见。

## 功能效果指标

| 指标 | 目标 | 统计位置 |
| --- | --- | --- |
| 根因 Top-1 命中率 | >= 70% | `packages/evals` |
| 根因 Top-3 命中率 | >= 90% | `packages/evals` |
| 高风险动作拦截率 | 100% | guardrail tests + eval |
| Runbook 证据引用覆盖率 | 100% | diagnosis result validation |
| JSON 结构化输出合法率 | >= 98% | Agent parser metrics |
| 固定评测样本数量 | >= 20 | eval dataset |

## 性能指标

| 指标 | 目标 |
| --- | --- |
| `POST /api/alerts` P95 | <= 300 ms |
| 非 Agent 查询接口 P95 | <= 200 ms |
| 单次 demo 诊断 P95 | <= 60 s |
| 单个工具调用超时 | <= 5 s |
| 工具重试次数 | <= 2 |
| Runbook top 5 检索 P95 | <= 2 s |

## 可靠性指标

- Celery task 可重试，重试不重复创建 incident、action、approval。
- 同一个未关闭 fingerprint 去重成功率 100%。
- 工具失败不导致 Agent run 崩溃，必须记录 degraded result。
- L2/L3 无审批执行成功率为 0%。
- L4 动作直接拒绝，不能进入审批。

## 质量指标

- 后端整体覆盖率 > 80%。
- `packages/agent`、`packages/tools`、`packages/rag`、`packages/db` 覆盖率目标 >= 85%。
- `packages/agent/guardrails` 覆盖率目标 >= 95%。
- 前端 statements、branches、functions、lines 均 > 80%。
- Ruff、mypy、pytest、Vitest、Playwright smoke 全部通过。

## Token 与上下文效率指标

| 指标 | 目标 |
| --- | --- |
| Provider prompt cache 命中率 | >= 60%，完整 eval 后逐步优化到 >= 75%；如 provider 不返回该指标则标记 unknown |
| App prompt segment cache 命中率 | >= 70% |
| Runbook chunk cache 命中率 | >= 70% |
| 工具结果摘要复用率 | >= 50% |
| 单次诊断输入 token P95 | 控制在模型上下文窗口的 40% 以内 |
| 压缩后上下文证据保留率 | >= 95%，按 evidence id 计算 |
| 历史记忆误用率 | <= 5%，评测中不相关历史不能影响根因 |

## 指标落库字段

新增或复用以下表：

- `agent_runs.total_prompt_tokens`
- `agent_runs.total_completion_tokens`
- `agent_runs.provider_cache_hit_count`
- `agent_runs.provider_cache_miss_count`
- `agent_runs.app_cache_hit_count`
- `agent_runs.app_cache_miss_count`
- `tool_calls.cache_key`
- `tool_calls.cache_hit`
- `memory_events.event_type`
- `memory_events.before_tokens`
- `memory_events.after_tokens`
- `memory_events.compression_ratio`

## CI 门禁

- `pytest --cov-fail-under=80`
- `npm run test:coverage`
- `npm run test:e2e`
- `python -m packages.evals.runner --suite smoke`
- schema snapshot diff 必须为空，除非主动更新契约。
