# Guardrail 与审批

## 原则

Guardrail 是确定性规则，不信任 LLM 决定最终执行权限。LLM 可以提出动作和 risk hint，但最终等级由 `packages/agent/guardrails/policy.py` 分类。

## 风险表

| action type | 风险 | 是否审批 | 说明 |
| --- | --- | --- | --- |
| `query_metrics` | L0 | 否 | read-only metrics |
| `query_logs` | L0 | 否 | read-only logs |
| `query_traces` | L0 | 否 | read-only traces |
| `query_git` | L0 | 否 | read-only git |
| `create_ticket` | L1 | 否 | ticket creation |
| `generate_report` | L1 | 否 | report generation |
| `warmup_cache` | L1 | 否 | cache warming |
| `adjust_connection_pool` | L1 | 否 | pool tuning |
| `restart_pod` | L2 | 是 | pod restart |
| `scale_deployment` | L2 | 是 | scaling |
| `restart_service` | L2 | 是 | service restart |
| `enable_rate_limit` | L3 | 是 | rate-limit change |
| `rollback_release` | L3 | 是 | rollback |
| `scale_back` | L2 | 是 | scale back after bad scale |
| `revert_config` | L2 | 是 | revert config change |
| `cancel_deployment` | L3 | 是 | cancel in-progress deployment |
| `delete_data` | L4 | 否 | direct reject |
| `truncate_table` | L4 | 否 | direct reject |
| `flush_cache` | L4 | 否 | direct reject |
| `modify_database` | L4 | 否 | direct reject |

未知动作默认按 L2 处理，要求审批。

## 禁止关键词

Guardrail 会检查 action type、target 和 params 中的危险 token：

- `delete`
- `drop`
- `truncate`
- `modify_database`
- `flush`

命中后升级为 L4 并拒绝。匹配使用 token 边界，避免把 `dropdown`、`deleted` 等普通词误判。

## risk_hint

模型给出的 `risk_hint` 只能提高风险，不能降低风险。若模型 hint 为 L3/L4 且高于规则分类，则采用更高风险。

## Guardrail 节点输出

`guardrail_check` 会为每个 action 写入：

- `risk_level`
- `allowed`
- `requires_approval`

并设置内部路由字段：

- `_needs_approval`
- `_all_l4`

## 审批节点

`human_approval` 只处理 `requires_approval=True` 的动作。

首次进入时：

1. 写入 `actions`，状态为 `waiting_approval`。
2. 写入 `approvals`，状态为 `waiting`。
3. 将 approval IDs 写入 state。
4. 写节点轨迹，状态为 `waiting_approval`。
5. 调用 LangGraph `interrupt()`。

审批完成后：

1. API 更新 approval/action。
2. API 入队 `resume_incident_after_approval`。
3. Worker 使用同一 checkpoint config 恢复 graph。
4. `human_approval` 重新读取数据库中的每个 approval 状态。
5. 已 approve 的 action 才允许进入 `execute_action`。
6. rejected action 会标记 `allowed=False`。
7. 仍 waiting 的 action 不会被执行。

## L3 二次确认

L3 approve 必须满足：

```text
risk_ack == true
confirm_action_type == action.type
confirm_target == action.target
```

不满足时返回 400，不能更新 approval 为 approved。

## 无 checkpointer 场景

测试或内存数据库可能没有 PostgreSQL checkpointer。实现允许 dev/test 自动审批部分动作，但有两个限制：

- L3 永远不会 auto-approved。
- 真实数据库下 checkpointer 初始化失败会 fail closed，不继续运行。

这样可以避免审批 gate 故障导致 L2/L3 被绕过。

## 拒绝与 replan

如果审批全部拒绝，图会进入 replan。为了防止确定性 planner 重复提出同样动作导致无限循环，reject -> replan 最多 3 次，超过后直接生成报告。

## 执行后端

`execute_action` 只执行：

```text
allowed == true
requires_approval == false
```

默认执行结果来自 `FixtureExecutorBackend` 的固定映射，不调用真实系统。`EXECUTOR_BACKEND=live` 是显式 opt-in，只能执行受支持的 Kubernetes restart、scale/scale_back 和 rollback 变更；其他 live action 必须失败关闭。

当前 fixture action 包括：

- `restart_pod`
- `restart_service`
- `scale_deployment`
- `scale_back`
- `revert_config`
- `rollback_release`
- `cancel_deployment`
- `enable_rate_limit`
- `warmup_cache`
- `create_ticket`
- `adjust_connection_pool`

`take_snapshot` 在动作前保存可回滚基线。若 verify 判定 `degraded`，回滚类动作会使用原始 `pre_action_snapshot` 中的具体 revision、replicas 等参数。L4 永远不执行。
