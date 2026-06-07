# Guardrail 与审批设计

## 目标

保证 Agent 不会因为模型输出而执行危险操作。模型只能提出建议，最终动作必须经过确定性规则判定。

## 风险分级策略

实现 `packages/agent/guardrails/policy.py`：

```python
class GuardrailDecision(BaseModel):
    action_id: str
    risk_level: Literal["L0", "L1", "L2", "L3", "L4"]
    allowed: bool
    requires_approval: bool
    reason: str
```

## 规则表

| action type | 默认等级 | 策略 |
| --- | --- | --- |
| `query_metrics` | L0 | 自动执行 |
| `query_logs` | L0 | 自动执行 |
| `query_traces` | L0 | 自动执行 |
| `create_ticket` | L1 | 自动执行 |
| `generate_report` | L1 | 自动执行 |
| `restart_pod` | L2 | 审批 |
| `scale_deployment` | L2 | 审批 |
| `enable_rate_limit` | L3 | 审批 + 二次确认 |
| `rollback_release` | L3 | 审批 + 二次确认 |
| `delete_data` | L4 | 拒绝 |
| `truncate_table` | L4 | 拒绝 |
| `flush_cache` | L4 | 拒绝 |
| `modify_database` | L4 | 拒绝 |

## 判定流程

1. 规范化 action type。
2. 检查是否命中禁止关键词。
3. 检查参数是否包含危险目标，如 `database`, `all`, `prod`。
4. 根据 action type 获取默认风险等级。
5. 如果模型给出的 risk_hint 高于默认等级，取更高风险。
6. L0/L1 自动放行。
7. L2 创建 approval。
8. L3 创建 approval，并要求审批请求包含 `risk_ack=true`、`confirm_action_type`、`confirm_target`。
9. L4 直接拒绝。

## 审批状态机

```text
proposed
  -> waiting_approval
  -> approved -> executing -> succeeded|failed
  -> rejected -> proposed_alternative|closed
blocked
```

## 审批 API 与恢复

审批通过：

1. 校验 approval 当前状态为 `waiting`。
2. 如果 action 是 L3，校验 `risk_ack=true`、`confirm_action_type == action.type`、`confirm_target == action.target`。
3. 更新 `approvals.status=approved`，并持久化二次确认字段。
4. 更新 `actions.status=approved`。
5. 入队 `resume_incident_after_approval`。
6. LangGraph 使用 `thread_id=agent_run_id` 从 checkpoint 恢复。

审批拒绝：

1. 更新 `approvals.status=rejected`。
2. 更新 `actions.status=rejected`。
3. 恢复 graph，让 `plan_actions` 生成替代建议或直接报告。

## 测试矩阵

必须参数化测试：

- 每个 action type 的风险等级。
- 模型输出未知 action type。
- L4 关键词混入 params。
- L2/L3 无 approval 执行。
- L3 approval 缺少二次确认字段。
- approve 重复提交。
- reject 后不执行原 action。
- checkpoint resume 不重复创建 action。

目标覆盖率：`packages/agent/guardrails` >= 95%。
