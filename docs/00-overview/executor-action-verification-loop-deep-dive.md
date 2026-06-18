# 执行器、动作能力与验证闭环技术深挖

**最后更新：** 2026-06-18

本文按当前代码说明一个 action 从 LLM/规则建议到最终报告之间的执行闭环。它补充 [Agent 工作流](../02-agent/workflow.md)、[护栏与审批](../02-agent/guardrails-and-approval.md)、[工具层](../03-tools/tool-layer.md) 和 [护栏与审批技术深挖](guardrail-approval-deep-dive.md)。

阅读本文后应能回答：

- 风险等级、action capability metadata 和 executor backend 各自负责什么。
- 为什么风险表不等于 live executor allowlist。
- 默认 fixture executor 和 opt-in live Kubernetes executor 的边界在哪里。
- `take_snapshot`、`execute_action`、`verify` 和 replan 如何构成安全闭环。
- 为什么 `/api/actions/{action_id}/execute` 当前不是 live Kubernetes 写入口。

## 代码入口

| 入口 | 职责 |
|------|------|
| `packages/agent/graph.py` | Guardrail、approval、snapshot、execute、verify、replan 的图路由 |
| `packages/agent/guardrails/policy.py` | 确定性 L0-L4 风险分类 |
| `packages/agent/actions/capabilities.py` | action capability registry、live backend、snapshot contract、verify gates |
| `packages/agent/nodes/human_approval.py` | Action/Approval 持久化、GraphInterrupt、resume 后逐项读取 DB 决策 |
| `apps/api/services/approval_service.py` | 单个/批量/email token 审批，L3 二次确认，resume 入队 |
| `packages/agent/nodes/take_snapshot.py` | 执行前 evidence count 和 K8s resource snapshot |
| `packages/agent/nodes/execute_action.py` | executable action 过滤、live preflight、executor 调用、执行结果写回 |
| `packages/agent/nodes/verify.py` | 只读 verify gates、fresh evidence、整体 verdict、replan 条件 |
| `packages/agent/nodes/plan_actions.py` | 拒绝/降级后的重规划提示与 snapshot context |
| `packages/tools/executor_backends.py` | `FixtureExecutorBackend`、`LiveK8sExecutorBackend`、executor factory |
| `apps/api/services/action_service.py` | 手动 action execute API，当前固定使用 fixture executor |

## 一句话模型

执行闭环不是“模型建议后直接执行”。当前路径是：

```text
plan_actions
  -> guardrail_check
  -> human_approval?        # L2/L3
  -> take_snapshot
  -> execute_action
  -> verify
  -> resolved/report or replan
```

三个层次分工固定：

| 层 | 解决的问题 | 不解决的问题 |
|----|------------|--------------|
| Guardrail risk policy | action 是 L0/L1/L2/L3/L4、是否允许、是否需要审批 | 不说明 live backend 是否支持真实执行 |
| Action capability metadata | action 属于哪类 capability、是否有 live k8s backend、需要哪些 snapshot/preflight/verify gates | 不代替审批，不直接调用 Kubernetes |
| Executor backend | 在 fixture 中返回确定性结果，或在 live 中执行极窄 Kubernetes mutation | 不决定风险等级，不批准 L2/L3，不执行 verify |

风险表大于 live allowlist。例如 `enable_rate_limit`、`cancel_deployment` 是 L3，`revert_config` 是 L2，但当前 capability 是 `local_or_fixture_only`，不会成为真实 Kubernetes 写入。Live executor 当前只允许受控 restart/pause/resume/scale/rollback Kubernetes mutation。

## Action 对象形态

同一个“动作”在不同阶段有不同对象形态：

| 阶段 | 对象 | 来源 | 说明 |
|------|------|------|------|
| 计划阶段 | `recommended_actions` 中的 dict | `plan_actions` | 模型或规则建议，尚无最终执行许可 |
| 护栏后 | 带 `risk_level`、`allowed`、`requires_approval` 的 dict | `guardrail_check` | 图路由依据 |
| 审批阶段 | `Action` row | `human_approval` 或 `execute_action` | API/front-end/report 可查询的业务记录 |
| 审批阶段 | `Approval` row | `human_approval` | L2/L3 operator decision source of truth |
| 执行阶段 | `ExecutionResult` | executor backend | status/message/details，写入 state 和 action row |
| 验证阶段 | gate verdict | `verify` | gate、required、verdict、status、summary、evidence_ids |

不要把 `recommended_actions` 当成数据库事实，也不要把 `agent_runs.state` 当成 checkpoint source of truth。恢复依赖 LangGraph checkpointer，Action/Approval 表是人工决策和展示的业务事实。

## Guardrail 路由

`guardrail_check` 只做确定性分类：

- L0/L1：允许且不需要审批，可进入 `take_snapshot -> execute_action`。
- L2/L3：允许但需要审批，进入 `human_approval`。
- L4：不允许且不进入 approval，直接走报告路径。
- 未知 action type：保守归类为 L2，需要审批。
- `delete`、`drop`、`truncate`、`modify_database`、`flush` 等禁用词按 token 边界命中后提升为 L4。
- `risk_hint` 只能升高到 L3/L4，不能降级。

模型、runbook、前端都不能决定最终执行权限。它们最多提供 action 建议或 operator 输入，最终门禁仍是 guardrail、approval 和 executor preflight。

## Capability Registry

`packages/agent/actions/capabilities.py` 是 live 执行前的静态 contract。核心字段包括：

| 字段 | 含义 |
|------|------|
| `category` | `read_only`、`record_only`、`local_or_fixture_only`、`live_mutating_reversible`、`live_mutating_bounded_irreversible`、`forbidden` |
| `live_backend` | 当前只有 `none` 或 `k8s` |
| `reversible` | 是否有可声明的 rollback action |
| `bounded_irreversible` | 是否是受控但不可承诺 undo 的 mutation |
| `rollback_action_type` | 降级回退时可用的 action type |
| `required_snapshot_paths` | live preflight 必须在 `pre_action_snapshot` 中找到的字段 |
| `preflight_checks` | action-specific 预检项目 |
| `verify_gates` | 执行后必须或可选运行的只读验证 |
| `risk_level_expectation` | 文档化的风险等级期望，用于测试和审查 |

当前分类要点：

- `query_metrics`、`query_logs`、`query_traces`、`query_git` 是 `read_only`。
- `create_ticket`、`generate_report` 是 `record_only`。
- `warmup_cache`、`adjust_connection_pool`、`enable_rate_limit`、`cancel_deployment`、`revert_config`、`increase_memory_limit` 等是 `local_or_fixture_only`。
- `restart_pod`、`restart_deployment`、`restart_service`、`restart_statefulset`、`pause_rollout`、`resume_rollout` 是 `live_mutating_bounded_irreversible`。
- `scale_deployment`、`scale_back`、`rollback_release`、`rollback_deployment` 是 `live_mutating_reversible`。
- `delete_data`、`truncate_table`、`flush_cache`、`modify_database` 是 `forbidden`。

这份 registry 是代码审查 live 写路径时的第一张表。新增真实写 action 不能只改 executor handler；还必须明确 capability、snapshot contract、preflight、verify gates、guardrail 风险、fixture 行为和测试。

## Fixture Executor

`FixtureExecutorBackend` 是默认 backend：

| 使用场景 | 行为 |
|----------|------|
| 本地 demo | 返回确定性 mock result，便于演示完整链路 |
| CI / 单元测试 / smoke eval | 不触发真实外部写入 |
| 未注入 executor 的节点测试 | `execute_action` fallback 到 fixture |
| 手动 action execute API | `ActionService.execute()` 当前固定使用 fixture executor |

Fixture executor 覆盖常见 action 类型，包括 restart、pause/resume、scale、rollback、限流、缓存预热、工单、连接池调整、扩容、回缩、取消部署等。未知 action 会返回 succeeded 的通用 mock execution result。这是测试便利，不是生产 live 执行许可。

## Live K8s Executor

`LiveK8sExecutorBackend` 只有在 `EXECUTOR_BACKEND=live` 时由 worker `_build_deps()` 构造。它当前只支持以下 Kubernetes mutation：

| Action | Kubernetes 写入 | 参数 | 风险 |
|--------|-----------------|------|------|
| `restart_pod` | patch Deployment pod template annotation | 无 | L2 |
| `restart_deployment` | 同 `restart_pod` | 无 | L2 |
| `restart_service` | 同 `restart_pod`，target 仍是 Deployment | 无 | L2 |
| `restart_statefulset` | patch StatefulSet pod template annotation | 无 | L2 |
| `pause_rollout` | patch Deployment `spec.paused=true` | 无 | L2 |
| `resume_rollout` | patch Deployment `spec.paused=false` | 无 | L2 |
| `scale_deployment` | patch Deployment scale subresource | `replicas`，0 到 50 的整数 | L2 |
| `scale_back` | patch Deployment scale subresource | `replicas`，0 到 50 的整数；降级回退可从 snapshot 填充 | L2 |
| `rollback_release` | Deployment rollback subresource | 可选 `to_revision`，正整数 | L3 |
| `rollback_deployment` | 兼容别名，规范化为 `rollback_release` | 可选 `to_revision`，正整数 | L3 |

Live executor 不支持：

- 真实云资源写入。
- 删除数据、truncate table、flush cache、修改应用数据库。
- 任意 Kubernetes patch、exec、delete、cordon、drain。
- 任意跨 namespace 扫描或写入。
- 用 `increase_memory_limit` 等 local/fixture-only action 做真实 K8s resource patch。

Live executor 自身也失败关闭：

- target 和 namespace 必须是非空 DNS-1123 label。
- namespace 优先使用 context namespace，其次 backend namespace，最后 `default`。
- `params` 必须是 object；BaseModel 会先转 dict。
- 每个 action 有独立参数白名单。
- restart/pause/resume 不接受任何 params。
- scale 只接受 `replicas`，并在调用 Kubernetes 前校验范围。
- rollback 只接受可选 `to_revision`，并在调用 Kubernetes 前校验正整数。
- Kubernetes client 初始化先尝试 in-cluster config，再尝试 kubeconfig；失败摘要会脱敏。
- 成功 details 只记录 resource、target、namespace、patch/subresource 和受控字段，不写 kubeconfig、token 或 raw Kubernetes object。

## Namespace 一致性

Snapshot、live execute 和 post-action K8s verify 使用同一个有效 namespace：

```text
EXECUTOR_K8S_NAMESPACE
  -> K8S_NAMESPACE
  -> default
```

`packages/agent/nodes/_k8s_targeting.py` 中的 `effective_executor_k8s_namespace()` 是 Agent 节点侧统一入口。`packages/tools/executor_backends.py` 中的 `_effective_live_executor_namespace()` 是 executor factory 侧统一入口。

live preflight 会校验 snapshot payload 中的 `namespace` 与 effective executor namespace 一致。namespace 不一致时不会调用 Kubernetes 写 API。

## Snapshot Contract

`take_snapshot` 在执行前运行，写入：

- `taken_at`
- 当前 action type 列表
- metrics/logs/traces evidence count
- K8s action 的 per-target snapshot
- 第一个 K8s target 的兼容字段 `k8s`

K8s snapshot 操作：

| Action | Snapshot operation |
|--------|--------------------|
| `restart_statefulset` | `get_statefulset` |
| 其他 K8s live action | `get_deployment` |

snapshot 失败不会让 graph 直接崩溃，但 live preflight 会因为缺少 required snapshot paths 或身份不匹配而阻断执行。这样可以让报告记录失败原因，同时避免在证据不足时真实写入。

如果上一轮 `verify_result == degraded`，且已有可用 snapshot，`take_snapshot` 会保留旧 snapshot，避免降级回退时把“执行前状态”覆盖成“执行后坏状态”。

## Execute Preflight

`execute_action` 只执行：

```python
action.get("allowed") and not action.get("requires_approval")
```

对 live backend，还必须通过 `_live_preflight_block()`：

1. action 必须在 capability registry 中注册。
2. capability 必须声明 `live_backend="k8s"`。
3. live mutation 必须是 reversible 或 bounded irreversible。
4. live mutation 必须声明 verify gates。
5. reversible action 必须有已注册 rollback action，并且 live backend 有 rollback handler。
6. bounded irreversible action 必须有 preflight checks。
7. target 和 namespace 必须符合 Kubernetes 名称规则。
8. required snapshot paths 必须存在。
9. snapshot resource name 必须等于 action target。
10. snapshot namespace 必须等于 effective executor namespace。
11. action-specific preflight checks 必须全部通过。

Action-specific 检查包括：

- Deployment 或 StatefulSet snapshot 是否存在。
- 副本数是否大于 0。
- rollout 是否已失败。
- `pause_rollout` 执行前必须不是 paused。
- `resume_rollout` 执行前必须是 paused。
- restart/pause/resume 只能走对应受控 patch。
- scale params 只能包含 `replicas`，且范围合法。
- rollback params 只能包含可选 `to_revision`，且 revision 合法。

preflight 返回 blocked/failed 的 `ExecutionResult` 后不会调用 live executor handler。

## 审批与恢复

L2/L3 进入 `human_approval`：

```text
创建 Action(status=waiting_approval)
创建 Approval(status=waiting)
interrupt({type: approval_required, approval_ids})
```

API 审批路径的关键规则：

- `ApprovalService.approve()` / `reject()` 先提交 DB，再入队 resume。
- 同一 run 下仍有 waiting approval 时，不入队 resume。
- resume 后 `human_approval` 逐项读取 DB 中 approval 状态，不把一个决策套到整批 action。
- 全部拒绝会回到 `plan_actions`，最多 `MAX_REPLAN_CYCLES = 3`。
- L3 approve 必须有 `risk_ack=true`、`confirm_action_type == action.type`、`confirm_target == action.target or ""`。
- L3 不能通过 email token approve；L3 reject 可以通过 token，因为拒绝不扩大风险。
- 批量 approve 会先做全量 preflight，再修改任何 row；当前批量 approve 最多允许一个 L3。

无 checkpointer 的 dev/test 便捷路径只会自动批准 L2，不会自动批准 L3。真实 worker 路径不应在 PostgreSQL checkpointer 初始化失败时退回这个便捷路径。

## Verify Gates

`verify` 只对 L2/L3 execution result 做实质验证。L0/L1-only action 会返回 `skipped`。

gate plan 来自 execution result 中的 capability metadata；没有 capability gates 时回退到默认 `metrics_logs`。当前 gate：

| Gate | 数据源 | 说明 |
|------|--------|------|
| `metrics_logs` | `MetricsTool` + `LogsTool` | 只读最近窗口指标/日志，比较原始证据与 fresh evidence |
| `k8s_rollout` | `K8sDiagnosticsTool` | 只读 `rollout_status` 或 `get_statefulset` |
| `db_readonly` | `DbDiagnosticsTool` | 只读 `connection_pool`，rollback 类 action 默认附带但 optional |

gate verdict 字段包括：

- `gate`
- `required`
- `action_type`
- `target`
- `action_id`
- `verdict`
- `status`
- `summary`
- `evidence_ids`

Fresh evidence 会立即持久化，并把 `evidence_id` 回写到 gate verdict。Verify gate 永远只读，不触发新的写 remediation。

组合规则：

- required gate `degraded` 会让整体 `verify_result=degraded`。
- required gate `unchanged` 会让整体 `verify_result=unchanged`。
- required gate `unknown` 会让整体 `verify_result=unknown`。
- optional gate `unknown` 不阻止 resolved。
- optional gate 如果实际返回 `degraded` 或 `unchanged`，会参与整体判定。
- 任一 gate `improving` 且没有更差结果时，整体为 `improving`。
- 全部有效 gate resolved 时，整体为 `resolved`。

`k8s_rollout` 会按 action type 解释状态：

- `pause_rollout` 看到 paused 才能 resolved。
- `resume_rollout` 看到 paused 代表尚未恢复。
- `restart_statefulset` 使用 StatefulSet revision/replica status。
- scale action 会携带 expected replicas；只读 payload 的 desired/current replicas 不匹配时不能 resolved。

## Replan and Rollback

`verify` 后的路由：

| `verify_result` | 下一步 |
|-----------------|--------|
| `resolved` | `generate_report` |
| `skipped` | `generate_report` |
| `unknown` | `generate_report` |
| `error` | `generate_report` |
| 达到 `MAX_VERIFY_CYCLES = 2` | `generate_report` |
| `improving` / `unchanged` / `degraded` | `plan_actions` |

`plan_actions` 在 replan 时会看到 verify feedback 和 snapshot context。降级路径的提示规则强调：

- 不要重复提出刚造成 degraded 的动作。
- 优先基于 snapshot 规划 rollback 类动作。
- 可考虑 `scale_back`、`rollback_release`、`rollback_deployment`、`revert_config`。
- 不要猜测 snapshot 中不存在的具体值。

`execute_action` 在 `verify_result == degraded` 时只允许 rollback 类 action。非 rollback action 会 fail closed，避免降级后继续扩大影响面。

## 手动 Action Execute API

`POST /api/actions/{action_id}/execute` 当前由 `ActionService.execute()` 实现，并固定使用 `FixtureExecutorBackend`。它会重新检查：

- L4 永远 blocked。
- L2/L3 必须已有 approved approval。
- L3 approval 必须保存了二次确认字段，并且 type/target 匹配。
- action 不能已经处于 `executing` 或 `succeeded`。

通过后，它只写入 fixture execution result，适合受控演示和手动测试。真实 live Kubernetes executor 位于 worker 图执行路径，由 `EXECUTOR_BACKEND=live` 显式选择，并受 checkpoint、guardrail、approval、snapshot、preflight 和 verify 约束。

不要把手动 execute API 文档写成 live executor 入口。

## 常见误区

| 误区 | 正确理解 |
|------|----------|
| L2/L3 action 审批通过就一定真实写入 | 还要看 executor backend、capability、snapshot、preflight |
| 风险表中的所有 L2/L3 都能 live 执行 | 不是。很多 action 是 local/fixture-only |
| `restart_pod` 可回滚 | 不是。它是 bounded irreversible rolling restart，只能靠 verify/replan 处理后续恢复 |
| `scale_back` 总能自动知道原副本数 | 只有降级回退且 snapshot 有 replicas 时才可填充 |
| `db_readonly` gate 会修数据库 | 不会。它只执行 read-only diagnostics |
| `EXECUTOR_BACKEND=live` 可执行任意 Kubernetes patch | 不会。当前只允许受控 allowlist |
| API 直执 action 会走 live executor | 不会。当前 API 直执固定 fixture |
| optional gate 不重要 | optional `unknown` 不阻止 resolved，但 optional `degraded` / `unchanged` 会参与整体判定 |

## 新增或修改执行动作 Checklist

1. 在 `packages/agent/guardrails/policy.py` 增加或校准风险等级。
2. 在 `packages/agent/prompts.py` 更新 allowed action table，避免 planner 生成未知类型。
3. 在 `packages/agent/actions/capabilities.py` 明确 category、live backend、snapshot contract、preflight checks、verify gates。
4. 在 `FixtureExecutorBackend` 增加确定性结果。
5. 如果确实要支持 live，必须证明仍处于允许的 Kubernetes mutation 范围内，并补齐 handler、参数白名单、rollback handler。
6. 更新 `take_snapshot` 的 K8s operation 或 required fields。
7. 更新 `verify` gate 逻辑，保证验证只读且可解释。
8. 更新 API/前端展示和审批字段。
9. 补测试：guardrail、capability、executor backend、execute preflight、approval L2/L3/L4、verify/replan、API fixture direct execute。
10. 更新本文、[护栏与审批](../02-agent/guardrails-and-approval.md)、[工具层](../03-tools/tool-layer.md)、[配置参考](../11-reference/configuration.md) 和必要的部署/运维文档。

## Debug Checklist

审批后没有执行：

- 查 `approvals.status` 是否仍有同 run 的 `waiting`。
- 查 API 是否先 commit 再 enqueue resume。
- 查 `human_approval` resume 后是否把该 action 的 `requires_approval` 清掉。
- 查 action 是否 `allowed=true`。

live executor blocked：

- 查 worker 的 `EXECUTOR_BACKEND` 是否为 `live`。
- 查 target/namespace 是否 DNS-1123 合法。
- 查 `pre_action_snapshot` 是否存在并包含 required paths。
- 查 snapshot name/namespace 是否和 action/context 一致。
- 查 `execution_results[].details.blocked_reason` 或 preflight error。

verify 不 resolved：

- 查 `verify_gates` 中哪个 required gate 是 `degraded`、`unchanged` 或 `unknown`。
- 查 fresh evidence 是否持久化并回写 `evidence_ids`。
- 查 scale action 的 expected replicas 是否和只读 rollout payload 一致。
- 查 `k8s_rollout` 对 pause/resume/statefulset 的 action-specific 判定。

降级后仍提出非 rollback 动作：

- 查 `plan_actions` 是否收到 `verify_result=degraded` 和 snapshot context。
- 查 FakeLLM/真实 provider 输出是否违反 degraded rules。
- 查 `execute_action` 是否 fail closed 了非 rollback action。

## 相关测试入口

按变更范围选择测试；Codex 不直接运行测试套件，用户本地运行后回贴结果：

```bash
pytest tests/unit/test_guardrails.py tests/unit/test_action_capabilities.py tests/unit/test_executor_backends.py -v
pytest tests/unit/test_agent_nodes.py -k "execute or verify or snapshot" -v
pytest tests/integration/test_approval_api.py tests/integration/test_graph_flow.py -v
```

前端涉及 L3 二次确认或 action 展示时，再运行：

```bash
cd apps/web
npm run test:coverage
```
