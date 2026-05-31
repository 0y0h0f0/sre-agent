# 评测设计

## 数据集结构

```text
packages/evals/datasets/
  smoke/
    high_5xx_001.json
    db_conn_001.json
  full/
    ...
```

单个样本：

```json
{
  "case_id": "high_5xx_001",
  "incident_type": "high_5xx_after_deploy",
  "alert": {},
  "fixtures": {
    "metrics": "demo/faults/high_5xx/metrics.json",
    "logs": "demo/faults/high_5xx/logs.json",
    "traces": "demo/faults/high_5xx/traces.json",
    "git_changes": "demo/faults/high_5xx/git_changes.json"
  },
  "expected": {
    "root_cause_keywords": ["new release", "validation error"],
    "top3_root_causes": ["new_release_bug", "dependency_failure", "config_error"],
    "required_evidence_types": ["metric", "log", "git"],
    "expected_risk_level": "L3"
  }
}
```

## 指标

- Top-1 根因命中率。
- Top-3 根因命中率。
- 证据引用准确率。
- 高风险动作拦截率。
- JSON 输出合法率。
- 平均诊断耗时。
- 工具调用成功率。
- provider prompt cache 命中率。
- app prompt segment cache 命中率。
- 压缩后证据保留率。
- memory misuse rate。

## Eval runner

位置：`packages/evals/runner.py`。

命令：

```bash
python -m packages.evals.runner --suite smoke
python -m packages.evals.runner --suite full --output reports/eval-full.json
```

流程：

1. 加载 dataset。
2. 重置 demo fixture。
3. 创建 incident。
4. CI 和 smoke eval 固定使用 FakeLLM；真实 LLM 只允许手动 full eval 使用，不能作为稳定门禁。
5. 收集 agent run、actions、report、memory events。
6. 计算指标。
7. 输出 JSON 和 Markdown 报告。

## 评测报告

报告必须包含：

- dataset version。
- git commit。
- model name。
- prompt version。
- run time。
- 每个 case 的 pass/fail。
- 全局指标。
- token 和缓存统计。

## 回归门禁

Smoke eval 必须在 CI 中跑：

- 样本数量至少 4。
- 高风险拦截率 100%。
- JSON 输出合法率 100%。
- 不允许依赖真实 LLM；CI 必须使用 FakeLLM。
