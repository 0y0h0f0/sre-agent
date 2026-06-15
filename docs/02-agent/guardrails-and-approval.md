# 护栏与审批

**最后更新：** 2026-06-15

## 概述

护栏是确定性安全层，实现在 `packages/agent/guardrails/policy.py` 和 `packages/agent/nodes/guardrail_check.py`。LLM 只能提出动作，不能决定动作是否允许执行。

审批由 `human_approval` 节点和 `apps/api/services/approval_service.py` 协作完成。L2/L3 动作先进入 approval，L3 还必须提供二次确认字段。L4 动作直接拒绝，不进入审批。

## 风险等级

| 等级 | 动作类型 | 是否允许 | 是否需要审批 | 当前动作 |
|------|----------|----------|--------------|----------|
| L0 | 只读查询 | 是 | 否 | `query_metrics`、`query_logs`、`query_traces`、`query_git` |
| L1 | 低风险本地/系统动作 | 是 | 否 | `create_ticket`、`generate_report`、`warmup_cache`、`adjust_connection_pool` |
| L2 | 服务/Kubernetes/资源限额运维动作 | 是 | 是 | `restart_pod`、`scale_deployment`、`restart_service`、`increase_memory_limit`、`scale_back`、`revert_config` |
| L3 | 回滚、限流、故障转移、部署取消 | 是 | 是，且二次确认 | `enable_rate_limit`、`raise_rate_limit`、`rollback_release`、`rollback_deployment`、`enable_circuit_breaker`、`switch_dns_resolver`、`failover`、`cancel_deployment` |
| L4 | 破坏性数据/缓存/数据库动作 | 否 | 否 | `delete_data`、`truncate_table`、`flush_cache`、`modify_database` |

未知 action type 默认归类为 L2，并要求审批。

## 分类规则

`classify_risk_level(action)` 的决策顺序：

1. 按 `_RISK_TABLE` 查 action type。
2. 未知动作使用保守默认：`L2`、`requires_approval=true`。
3. 扫描 action type、target、params 中的禁用词：`delete`、`drop`、`truncate`、`modify_database`、`flush`。
4. 禁用词按 token 边界匹配，命中后提升到 L4 并硬拒绝。
5. `risk_hint` 只能把风险升到更高的 `L3` 或 `L4`，不能降级。

这意味着模型不能通过把 `risk_hint` 写成 `L0` 来绕过审批，也不能把带 `drop`、`truncate`、`flush` 等词的动作伪装成安全动作。

## Guardrail 节点输出

`guardrail_check` 会在每个 `recommended_actions` 元素上补充：

| 字段 | 含义 |
|------|------|
| `risk_level` | `L0` 到 `L4` |
| `allowed` | L4 为 `false`，其余为 `true` |
| `requires_approval` | L2/L3 为 `true`，L0/L1/L4 为 `false` |

同时写入内部路由字段：

| 字段 | 含义 |
|------|------|
| `_needs_approval` | 至少一个动作需要审批 |
| `_all_l4` | 本批动作全部是 L4，直接转报告 |

## 审批流程

```text
plan_actions
  -> guardrail_check
  -> human_approval
       -> 创建 Action(status=waiting_approval)
       -> 创建 Approval(status=waiting)
       -> interrupt({type: approval_required, approval_ids})
  -> API approve/reject 写 DB 并提交事务
  -> Celery resume_agent_run
  -> human_approval 读取每个 approval 的 DB 状态
       approved -> 清除该 action 的 requires_approval
       rejected -> allowed=false
       waiting  -> 保持不可执行
  -> take_snapshot -> execute_action
```

关键点：

- API 决策先提交事务，再入队 resume，确保 worker 使用新连接时能读到审批状态。
- 同一 agent run 下还有 approval 为 `waiting` 时，不会 resume。批量动作必须全部被决定。
- resume 后按 action 逐个对齐 approval 状态，不会把单个 approve/reject 盲目应用到整个批次。
- 被 reject 的批次会回到 `plan_actions`，最多 3 轮；超过上限后生成报告。

## L3 二次确认

L3 审批必须满足：

```text
risk_ack == true
confirm_action_type == action.type
confirm_target == action.target
```

缺失或不匹配时，`ApprovalService.approve()` 返回 `ValidationAppError`，approval 仍保持 `waiting`。

邮件 token 审批不支持 L3 approve。`approve_by_token()` 遇到 L3 会拒绝，并要求使用 Web 控制台完成二次确认。L3 reject 可以通过 token 完成，因为拒绝不会扩大风险。

## 自动审批边界

当前实现里有两类容易混淆的“自动”行为：

| 场景 | 默认 | 边界 |
|------|------|------|
| 正常 worker + checkpointer | 人工审批 | L2/L3 都会进入 approval；L3 需要二次确认 |
| 无 checkpointer 的 dev/test 路径 | L2 可被测试便捷路径批准 | 仅用于没有 checkpoint 的测试/开发场景，L3 不会被自动批准 |
| stale approval Celery 任务 | 关闭 | `APPROVAL_AUTO_APPROVE_MINUTES=0` 时禁用；即使开启，也只允许 L0/L1/L2，L3+ 永不自动批准 |

`AUTOMATION_LEVEL` 属于 discovery/config proposal 控制面，不是 Agent remediation 的执行许可。动作执行仍以 guardrail、approval 和 executor backend 为准。

## 执行前快照

`take_snapshot` 在 `execute_action` 前运行，写入 `pre_action_snapshot`：

- `taken_at`
- action type 列表
- metrics/logs/traces evidence 数量
- 对 K8s 动作，读取 `get_deployment` 的 replicas、revision、image、ready/available replicas 等信息

当 `verify_result == degraded` 且下一轮计划包含 rollback 类动作时，如果已有可用 snapshot，会保留原 snapshot，避免回滚依据被新状态覆盖。

## 执行动作过滤

`execute_action` 只执行满足以下条件的动作：

```python
action.get("allowed") and not action.get("requires_approval")
```

执行后：

- L0/L1 自动动作如果还没有属于当前 incident/run 的 `action_id`，`execute_action` 会先创建 Action 记录并写回 state，再调用 executor；持久化失败时不执行该动作。
- 默认 backend 是 `FixtureExecutorBackend`。
- `EXECUTOR_BACKEND=live` 才会构造 `LiveK8sExecutorBackend`。
- 处于 degraded verify 循环时，只允许 rollback 类动作；非 rollback 动作会失败关闭。
- action status 更新是 best-effort，失败会记录日志但不丢弃已得到的执行结果。

## Live Executor 限制

`LiveK8sExecutorBackend` 只允许窄范围 Kubernetes mutation：

| 动作 | live 行为 |
|------|-----------|
| `restart_pod` | patch Deployment pod template annotation，触发 rolling restart |
| `restart_service` | 同 `restart_pod`，目标仍是 Deployment |
| `scale_deployment` | patch Deployment scale subresource |
| `scale_back` | patch Deployment scale subresource，用于回缩 |
| `rollback_release` | 调用 Deployment rollback subresource |
| `rollback_deployment` | 兼容别名，规范化为 `rollback_release`，调用同一个 Deployment rollback subresource |

其它动作在 live executor 中失败关闭。live executor 会校验 namespace 和 target 符合 Kubernetes DNS-1123 label，防止路径注入。

Live action capability metadata 还会在 `execute_action` 前执行确定性 preflight：

| 类别 | 动作 | 语义 |
|------|------|------|
| reversible | `scale_deployment`、`scale_back`、`rollback_release`、`rollback_deployment` | 必须有 rollback action、snapshot contract 和 verify gates |
| bounded irreversible | `restart_pod`、`restart_service` | 只允许 Deployment rolling restart patch；需要 snapshot、preflight 和 verify gates，但不提供 restore/undo 保证 |

`restart_pod` / `restart_service` 不应被文档、prompt 或 report 描述为“可完全恢复”或“可回滚”的动作。它们只是受审批、snapshot、preflight、verify/replan 和审计约束的 bounded irreversible 操作；如果验证降级，下一轮 planner 只能基于 snapshot 规划 `scale_back`、`rollback_release`、`rollback_deployment`、`revert_config` 等可执行回退/升级动作，不能假设 restart 本身有 undo。

`scale_deployment` 只表示调整 Deployment 副本数，参数使用 `replicas`。内存限额调整使用 `increase_memory_limit`，当前仅在 fixture/local 路径有确定性执行结果，不属于 live executor 的真实 Kubernetes mutation。

项目不允许新增真实云资源写操作，不允许删除数据、修改应用数据库、truncate table 或 flush 真实 cache。

## Verify Gate 边界

`verify` 节点只执行只读 gate：

| Gate | 数据源 | 边界 |
|------|--------|------|
| `metrics_logs` | Prometheus/Loki | 只读查询最近窗口 |
| `k8s_rollout` | `K8sDiagnosticsTool.rollout_status` | 只读 K8s diagnostics，不执行 rollout 写入 |
| `db_readonly` | `DbDiagnosticsTool.connection_pool` | 只读 PostgreSQL diagnostics，不触发 DB remediation |

Required gate 来自静态 action capability registry。Action `params` 只能把 optional gate 升级为 required，不能把 capability-required gate 降级为 optional；模型不能通过 params 放宽 verify policy。

## 与 Runbook 动作分类的关系

`packages/rag/runbook_action_classifier.py` 用于审查 LLM runbook 草稿中的动作措辞，分类为 `read_only`、`diagnostic_only`、`approval_required`、`forbidden`、`unknown`。它不替代 Agent 执行路径的 guardrail。

最终执行权限始终由 `classify_risk_level()` 和 approval 状态决定。

## 新增动作类型 checklist

1. 在 `packages/agent/guardrails/policy.py` 中加入风险等级。
2. 在 `packages/agent/prompts.py` 的 allowed action table 中加入动作，避免 planner 使用未知类型。
3. 如果是可执行动作，在 fixture executor 中提供确定性结果。
4. 如果要支持 live executor，必须保持 opt-in，并证明不扩大已允许的 live K8s mutation 范围。
5. 更新 API/前端审批展示和测试。
6. 增加单元测试覆盖 L2/L3/L4、未知动作、禁用词、`risk_hint` 升级和 L3 二次确认。

## 相关测试

- `tests/unit/test_guardrails.py`
- `tests/unit/test_agent_nodes.py`
- `tests/unit/test_approvals.py`
- `tests/unit/test_live_executor_backend.py`
- `tests/unit/test_action_execution.py`
- `tests/integration/test_approval_resume.py`
