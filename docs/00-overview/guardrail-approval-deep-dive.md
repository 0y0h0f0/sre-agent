# 护栏与审批技术深挖

**最后更新：** 2026-06-18

本文从代码路径解释一次推荐动作如何被确定性护栏分类、进入人工审批、恢复 LangGraph、执行 fixture/live backend，并在失败或拒绝时回到安全路径。它补充 [护栏与审批](../02-agent/guardrails-and-approval.md)：专题文档定义规则，本文解释规则在运行时如何组合。

如果需要进一步聚焦 action capability metadata、pre-action snapshot、executor backend、verify gates、degraded rollback/replan 和手动 action execute API 的 fixture-only 边界，见 [执行器、动作能力与验证闭环技术深挖](executor-action-verification-loop-deep-dive.md)。

## 阅读目标

读完本文应能回答：

- LLM 推荐动作为什么不能直接执行。
- L2/L3/L4 在代码中如何被判定。
- 批量审批、邮件 token 和 stale auto-approve 分别有哪些边界。
- 审批后为什么不会只批准一个动作就提前执行整批动作。
- `EXECUTOR_BACKEND=live` 打开后，还需要哪些前置检查才会调用 Kubernetes 写 API。

## 代码入口

| 主题 | 入口 |
|------|------|
| 风险分类 | `packages/agent/guardrails/policy.py` |
| Guardrail 节点 | `packages/agent/nodes/guardrail_check.py` |
| Approval interrupt/resume | `packages/agent/nodes/human_approval.py` |
| Approval API service | `apps/api/services/approval_service.py` |
| Approval HTTP router | `apps/api/routers/approvals.py` |
| Approval schema | `apps/api/schemas/approvals.py` |
| 执行前快照 | `packages/agent/nodes/take_snapshot.py` |
| 动作执行节点 | `packages/agent/nodes/execute_action.py` |
| action capability metadata | `packages/agent/actions/capabilities.py` |
| fixture/live executor | `packages/tools/executor_backends.py` |
| stale auto-approve task | `apps/worker/tasks.py` 的 `auto_approve_stale_approvals` |
| Beat 调度 | `apps/worker/celery_app.py` |

## 关键数据对象

| 对象 | 表/模型 | 作用 |
|------|---------|------|
| `Action` | `actions` | 存储动作类型、目标、参数、风险等级、审批/执行状态和执行结果。 |
| `Approval` | `approvals` | 存储审批批次中的单个审批项、决策人、评论、L3 二次确认字段和 email token。 |
| `AgentRun` | `agent_runs` | 保存 run 状态和展示快照；真正恢复依赖 LangGraph checkpoint。 |
| `AgentRunNode` | `agent_run_nodes` | 记录 `guardrail_check`、`human_approval`、`execute_action` 等节点轨迹。 |
| `AuditLog` | `audit_logs` | 记录 approve/reject/auto-approve 的审计事件。 |
| `Incident` | `incidents` | 承载服务名、状态和最终诊断/报告展示字段。 |

`agent_runs.state` 只是展示/debug 快照，不是审批恢复的 source of truth。审批恢复时，`human_approval` 会重新读取 `approvals` 和 `actions`。

## 端到端路径

```text
plan_actions
  -> guardrail_check
       classify_risk_level(action)
       action.risk_level / allowed / requires_approval
  -> human_approval
       L2/L3: create Action + Approval
       interrupt({type: approval_required, approval_ids})
  -> API approve/reject/batch/token
       validate waiting status
       validate L3 confirmation when approving L3
       write approval/action/audit
       commit
       enqueue resume only when no approval in run is waiting
  -> resume_incident_after_approval
  -> human_approval
       reconcile each approval from DB
       approved: clears requires_approval
       rejected: allowed=false and replan budget increments
  -> take_snapshot
  -> execute_action
       execute only allowed and not requires_approval
       fixture by default, live only when explicitly configured
  -> verify
       read-only gates
       resolved or bounded replan/report
```

## 1. Deterministic Risk Classification

`classify_risk_level(action)` 是执行权限的第一道确定性规则。它不信任 LLM，也不信任前端。

当前风险表的核心含义：

| 等级 | 行为 |
|------|------|
| `L0` | 只读查询，自动执行。 |
| `L1` | 低风险本地/记录类动作，自动执行。 |
| `L2` | 服务/Kubernetes/资源类运维动作，需要审批。 |
| `L3` | 回滚、限流、故障转移、部署取消，需要审批和二次确认。 |
| `L4` | 破坏性动作，直接拒绝，不进入审批。 |

分类顺序：

1. 按 `_RISK_TABLE` 查 action type。
2. 未知 action type 使用保守默认：`L2`、`requires_approval=true`。
3. 扫描 action type、target、params 中的禁用词：`delete`、`drop`、`truncate`、`modify_database`、`flush`。
4. 禁用词按 token 边界匹配；命中后提升为 `L4` 并硬拒绝。
5. `risk_hint` 只能升级到更高的 `L3` 或 `L4`，不能降级。

两个细节容易误读：

- `all` 和 `prod` 不是禁用词。生产安全靠 L2/L3 审批和 live executor 预检保证，不靠粗暴字符串拦截。
- capability registry 中有些动作是为了 runbook 审查或 planner 元数据存在；是否能真实执行仍取决于 guardrail 表、approval 状态和 executor 支持。

## 2. Guardrail Node Routing

`guardrail_check` 对 `recommended_actions` 中每个动作补齐：

- `risk_level`
- `allowed`
- `requires_approval`
- `guardrail_reason`

同时写入两个内部路由字段：

| 字段 | 含义 |
|------|------|
| `_needs_approval` | 至少一个动作需要审批，图会进入 `human_approval`。 |
| `_all_l4` | 本批动作全部是 L4，图跳过审批和执行，直接生成报告。 |

这意味着 L4 不会创建可批准的 approval；混合批次中，L4 会保留 `allowed=false`，可审批的 L2/L3 仍按审批路径处理。

## 3. Approval Batch and Interrupt

`human_approval` 只处理 `requires_approval=true` 的动作。

首次进入时：

1. 为每个待审批动作创建 `Action(status=waiting_approval)`。
2. 为每个 action 创建 `Approval(status=waiting)`。
3. 将 `approval_ids` 写入 state 的 `approval_status`。
4. 写入 node trace，状态为 `waiting_approval`。
5. 通过 LangGraph `interrupt()` 暂停，payload 为 `{type: approval_required, approval_ids}`。

恢复时有两个保护：

- 如果 checkpoint state 稀疏或节点被重新运行，`_recover_existing_approval_batch()` 会从 DB 找回当前批次，避免重复创建 approval。
- `_apply_db_decisions()` 按每个 approval 的 DB 状态对齐 action，不会把一次 approve/reject 盲目套到整批动作。

没有 checkpointer 的 dev/test 路径会走便捷自动批准，但只适用于无真实 checkpoint 的开发/测试场景，且 L3 不会被自动批准。真实 PostgreSQL checkpointer 构造失败时，worker 会 fail closed，而不是退回到自动批准路径。

## 4. Approval API Decisions

`ApprovalService.approve()` 和 `reject()` 的共同规则：

- 先用 `get_for_update()` 锁定 approval；如果数据库不支持或测试场景回退，再普通查询。
- approval 必须是 `waiting`，否则返回冲突。
- 更新 approval 和 action 状态。
- 写 audit log。
- 先 `commit()`，再尝试 resume，确保 worker 使用新连接时能读到审批结果。
- `_maybe_resume()` 只有在同一个 run 下没有 `waiting` approval 时才入队恢复。

L3 approve 额外要求：

```text
risk_ack == true
confirm_action_type == action.type
confirm_target == action.target
```

这些字段会持久化到 `approvals`，便于审计和前端回显。L3 reject 不需要二次确认，因为拒绝不会扩大执行风险。

## 5. Batch Approval

`POST /api/approvals/batch` 使用 `BatchApprovalRequest`，一次最多 50 个 approval。

批量决策的关键语义是先预检后写入：

1. `approval_ids` 必须唯一。
2. 所有 approval 必须存在且仍是 `waiting`。
3. 每个 approval 必须能找到对应 action。
4. 如果批量 approve 中包含 L3，整批请求最多只能包含一个 L3 approval，并且必须提供匹配的 L3 二次确认字段。
5. 只有全部预检通过，才会写 approval/action/audit。

这样避免了部分成功：例如一个 L2 已被批准，但同批 L3 因确认字段错误失败，导致同一 run 的动作状态不一致。

## 6. Email Token Boundary

邮件 token 是单次使用的审批入口：

- `generate_email_token()` 生成 24 小时有效 token。
- `_get_approval_by_token()` 会检查过期；过期 token 会被清空并返回校验错误。
- `approve_by_token()` 不允许批准 L3，要求回到 Web 控制台填写二次确认。
- `reject_by_token()` 可以拒绝 L3，因为拒绝不会增加风险。
- token 决策成功后会清空 token。
- token 页面支持 redirect，但 router 只允许相对路径，拒绝外部 URL，防止 open redirect。

## 7. Stale Auto-Approve Boundary

Celery Beat 每 60 秒调度 `auto_approve_stale_approvals`，但任务是否实际批准由配置控制：

| 配置 | 默认 | 含义 |
|------|------|------|
| `APPROVAL_AUTO_APPROVE_MINUTES` | `0` | 0 表示禁用 stale auto-approve。 |
| `APPROVAL_AUTO_APPROVE_MAX_RISK` | `L2` | 若启用，最高只允许 L0/L1/L2。 |

任务内还有硬边界：

- `threshold_minutes <= 0` 直接返回 disabled。
- `max_risk` 超过 L2 时直接 skipped。
- L3+ 永不自动批准。
- 只有全部 approval 都不再 waiting 的 run 才会被 resume。

Kubernetes base configmap 当前把 `APPROVAL_AUTO_APPROVE_MINUTES` 设为 `"0"`，并把 `APPROVAL_AUTO_APPROVE_MAX_RISK` 设为 `"L1"`，比 Python 默认更保守。

## 8. Execution Gate

`execute_action` 只执行满足以下条件的动作：

```python
action.get("allowed") and not action.get("requires_approval")
```

执行前还会处理几件事：

- L0/L1 自动动作如果没有 `action_id`，会先创建 Action 记录；持久化失败则不执行。
- 没有注入 executor 时使用 `FixtureExecutorBackend`。
- 处于 `verify_result == degraded` 的下一轮时，只允许 rollback 类动作；非 rollback 动作 fail closed。
- 执行结果会 best-effort 写回 `actions.status` 和 `execution_result`。

因此 approval 状态不是 UI 侧的展示字段，而是进入 executor 前的实际门禁。

## 9. Live Executor Preflight

默认 executor 是 fixture。只有 `EXECUTOR_BACKEND=live` 时，worker 依赖构造才会使用 `LiveK8sExecutorBackend`。

live 路径在真正调用 Kubernetes API 前还要通过 `execute_action` 的 preflight：

1. backend 名称必须是 `live`。
2. action 必须在 capability registry 中存在。
3. capability 必须声明 `live_backend="k8s"`，不能是 read-only、record-only 或 local-only。
4. live mutation 必须是 reversible 或 bounded irreversible，并且声明 verify gates。
5. Kubernetes target 和 namespace 必须符合 DNS-1123 名称规则。
6. `take_snapshot` 必须提供 capability 要求的 snapshot 字段。
7. snapshot 中的资源 name/namespace 必须与 action/context 匹配。
8. action-specific preflight checks 必须通过。

当前 live executor 只支持以下 Kubernetes mutation：

| 动作 | 真实写入 |
|------|----------|
| `restart_pod` / `restart_deployment` / `restart_service` | patch Deployment pod template annotation，触发 rolling restart。 |
| `restart_statefulset` | patch StatefulSet pod template annotation，触发 rolling restart。 |
| `pause_rollout` | patch Deployment `spec.paused=true`。 |
| `resume_rollout` | patch Deployment `spec.paused=false`。 |
| `scale_deployment` / `scale_back` | patch Deployment scale subresource，replicas 必须是 0 到 50 的整数。 |
| `rollback_release` / `rollback_deployment` | Deployment rollback subresource；`rollback_deployment` 会规范化为 `rollback_release`。 |

不支持真实云资源写入，不支持删除数据，不支持修改应用数据库，不支持 truncate table，不支持 flush 真实 cache。

## 10. Snapshot and Verify

`take_snapshot` 在 `execute_action` 前运行，用只读工具记录执行前状态：

- action type 列表
- evidence 计数
- `taken_at`
- 对 K8s action，按 target 获取 Deployment 或 StatefulSet snapshot

live preflight 依赖 snapshot 的身份和字段完整性。缺少 snapshot 或 snapshot 与目标不匹配时，不会调用 live backend。

`verify` 节点只执行只读 gate，例如 `metrics_logs`、`k8s_rollout`、`db_readonly`。Action `params` 只能把 optional gate 升级成 required，不能把 capability-required gate 降级。

## 11. Debug Checklist

审批后未继续：

- 查 `approvals` 是否仍有同 run 的 `waiting` 行。
- 查 API 是否先 commit 后 enqueue resume。
- 查 Celery worker 是否收到 `resume_incident_after_approval`。
- 查 `agent_run_nodes` 中 `human_approval` 是否再次运行。

审批创建重复：

- 查 checkpoint 是否稀疏或恢复异常。
- 查 `_recover_existing_approval_batch()` 是否能按 action type、target、risk level 找回旧 batch。
- 查同 run 的 `actions` / `approvals` 是否有多批 waiting。

L3 审批失败：

- 查请求体是否包含 `risk_ack=true`。
- 查 `confirm_action_type` 是否完全等于 `actions.type`。
- 查 `confirm_target` 是否完全等于 `actions.target` 或空字符串。

live executor 没有执行：

- 查 `EXECUTOR_BACKEND` 是否真为 `live`。
- 查 action 是否已通过 approval，且 state 中 `requires_approval=false`。
- 查 `pre_action_snapshot` 是否包含 capability 要求字段。
- 查 target/namespace 是否符合 DNS-1123。
- 查 `execution_result.details` 中的 blocked reason。

## 不要破坏的边界

- 不要让 LLM、runbook 或前端决定最终执行许可。
- 不要让 L4 进入 approval。
- 不要让 L3 通过 email approve 或 stale auto-approve。
- 不要在 sibling approval 仍 waiting 时 resume 并执行部分动作。
- 不要在真实 PostgreSQL checkpointer 构造失败时退回无 checkpoint 自动批准。
- 不要扩大 live executor 的 Kubernetes mutation 范围。
- 不要新增真实云资源写入、应用数据库写入、数据删除或真实 cache flush。

## 相关测试入口

按变更范围选择测试，不需要每次都跑全量：

- `tests/unit/test_guardrails.py`
- `tests/unit/test_action_capabilities.py`
- `tests/unit/test_executor_backends.py`
- `tests/unit/test_runbook_action_classifier.py`
- `tests/integration/test_approval_api.py`
- `tests/integration/test_phase6_collaboration.py`
- `tests/integration/test_graph_flow.py`
- `tests/integration/test_transaction_visibility.py`

示例命令：

```bash
pytest tests/unit/test_guardrails.py tests/unit/test_action_capabilities.py tests/unit/test_executor_backends.py -v
pytest tests/integration/test_approval_api.py tests/integration/test_phase6_collaboration.py -v
```
