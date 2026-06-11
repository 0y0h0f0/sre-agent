# Agent 工作流

## 入口

Agent 由 Celery worker 调用，不由 FastAPI inline 执行。

Worker 中的主要入口：

- `run_incident_diagnosis_logic(db, incident_id, agent_run_id)`
- `AgentRunner.run(incident_id, agent_run_id, alert_payload)`
- `AgentRunner.resume(agent_run_id, decision)`

LangGraph config 固定为：

```python
{
    "configurable": {
        "thread_id": agent_run_id,
        "checkpoint_ns": "",
    }
}
```

`thread_id` 必须等于 `agent_run_id`。审批恢复必须使用同一个 config。

## 当前节点顺序

当前实现的图位于 `packages/agent/graph.py`：

```text
parse_alert
  -> collect_metrics
  -> collect_logs
  -> collect_traces
  -> collect_deployment
  -> collect_k8s
  -> collect_db
  -> retrieve_memory
  -> cross_incident
  -> retrieve_runbook
  -> build_context
  -> diagnose
  -> compress_context
  -> conditional:
       missing evidence and cycle budget remains -> collect_gap -> build_context
       otherwise -> rank_hypotheses
  -> rank_hypotheses
  -> plan_actions
  -> guardrail_check
  -> conditional:
       L0/L1 -> take_snapshot -> execute_action
       L2/L3 -> human_approval
       L4    -> generate_report
  -> conditional after approval:
       approved -> take_snapshot -> execute_action
       rejected -> plan_actions, bounded by replan cap
       otherwise -> generate_report
  -> verify
  -> conditional:
       resolved/unknown/max cycles -> generate_report
       improving/unchanged/degraded -> plan_actions
  -> generate_report
  -> persist_memory
  -> END
```

相对 MVP 规划，当前实现增加了：

- `collect_k8s`
- `collect_db`
- `cross_incident`
- `compress_context`
- `persist_memory`

这些节点是只读诊断、上下文控制和记忆写回能力，不改变 mock executor 的安全边界。

## `IncidentState`

`IncidentState` 是流经图的 TypedDict。关键字段：

| 字段 | 含义 |
| --- | --- |
| `incident_id`、`agent_run_id` | public ID |
| `alert_payload` | 归一化后的告警 payload |
| `service_name`、`severity`、`alert_name`、`time_window` | 从告警解析出的上下文 |
| `metrics_evidence`、`logs_evidence`、`traces_evidence` | 观测证据 |
| `deployment_evidence` | 部署/Git 证据 |
| `k8s_evidence`、`db_evidence` | 只读扩展诊断证据 |
| `runbook_context` | Runbook RAG 结果 |
| `memory_context` | 记忆检索结果 |
| `cross_incident_context` | 相关事故上下文 |
| `hypotheses` | 候选根因 |
| `root_cause` | 最终根因 |
| `diagnosis_rationale` | 诊断推理摘要（reasoning 启用时有内容） |
| `cascade_analysis` | 级联故障分析结果（`is_cascade`、根服务、传播链） |
| `llm_calls` | LLM 调用和 token/cache 信息 |
| `recommended_actions` | 推荐动作 |
| `approval_status` | 审批状态 |
| `execution_results` | fixture/live executor 执行结果 |
| `pre_action_snapshot` | 动作执行前的证据与 K8s 部署快照，用于 verify 和 degraded 回滚 |
| `verify_result`、`verify_evidence` | 动作后验证结果和新鲜证据 |
| `incident_report` | 报告内容 |
| `token_budget` | token 预算和估算 |
| `compression_events` | 压缩事件 |
| `errors` | 节点错误 |
| `phase` | 当前阶段 |
| `_needs_approval`、`_all_l4`、`_needs_human_review` | guardrail 和证据路由内部字段 |

写入 `agent_runs.state` 前会移除 `_` 开头的内部字段，并将 datetime 转为 ISO 字符串。

## 依赖注入

所有节点接收 `AgentDeps`，包含：

- DB session。
- settings。
- request-local tool cache。
- metrics/logs/traces/git/k8s/db/runbook tools。
- memory store。
- context builder。
- LLM adapter。
- node tracer。
- tool call recorder。

节点不得自行创建 DB session，也不应直接构造真实外部 client。

## 节点追踪

每个节点应调用 `deps.node_tracer()` 写入：

- `agent_run_id`
- `name`
- `status`
- `started_at`
- `finished_at`
- `duration_ms`
- `input_summary`
- `output_summary`
- `error_message`

Worker 的 node tracer 还会通过 Redis 发布 WebSocket 节点事件。

## 工具调用审计

工具节点应通过 `deps.tool_call_recorder()` 写 `tool_calls`。记录字段包括：

- tool name。
- node name。
- input JSON 和 summary。
- ToolResult。
- cache key 和 cache hit。
- duration 和 error。

## 诊断与证据

`diagnose` 节点执行以下融合分析：

### 证据交叉验证

`packages/agent/evidence_validation.py` 将 metrics、logs、traces、deployment 信号按权重融合（Trace > Metrics > Logs > Git），计算 corroboration score：

- 多源一致：提高根因置信度。
- 信号冲突：设置 `state["_needs_human_review"]=True`。
- 缺失来源：降级但不阻断，deployment 缺失为中性信号。

### 级联故障分析

`packages/agent/topology.py` 基于服务依赖图（`SERVICE_TOPOLOGY_PATH` 配置或 trace 推导）进行分析：

- `analyze_propagation`：识别故障传播链，找到根服务。
- `correlate_incidents`：聚类同时发生的关联事故。
- 结果写入 `state["cascade_analysis"]`，单服务事故 `is_cascade=False`。

### LLM Reasoning

`packages/agent/llm/reasoning.py` 在 `LLM_REASONING_ENABLED=true` 时激活：

- 对 `LLM_REASONING_NODES` 中列出的节点启用深度推理（默认仅 `diagnose`）。
- 输出 `diagnosis_rationale` 摘要和 LLM 调用元数据。
- 不持久化原始 chain-of-thought 到数据库。

诊断输出必须引用 evidence ID 或 Runbook chunk ID。即使使用 FakeLLM，也应在后处理或证据验证阶段保留可追溯 ID。

大块原始日志不能直接进入 prompt。`build_context` 和 `compress_context` 需要控制 token 预算并记录压缩事件。

## 条件路由

`compress_context` 后路由：

- `missing_evidence` 非空且 collect-gap cycle 未超限：进入 `collect_gap` 后回到 `build_context`。
- 其他：进入 `rank_hypotheses`。

`guardrail_check` 后路由：

- `_all_l4=True`：直接进入 `generate_report`。
- `_needs_approval=True`：进入 `human_approval`。
- 其他：进入 `take_snapshot` 后再 `execute_action`。

`human_approval` 后路由：

- `phase == "approval_approved"`：进入 `take_snapshot` 后再 `execute_action`。
- `phase == "approval_rejected"`：最多 replan 3 次，超过后进入 report。
- 其他：进入 report。

`verify` 后路由：

- `resolved`、`unknown` 或 verify cycle 超限：进入 `generate_report`。
- `improving`、`unchanged`、`degraded`：回到 `plan_actions`。`degraded` 时 planner 只接收裁剪后的 `pre_action_snapshot` 摘要，执行回滚类动作时 executor 使用该快照补具体参数。

## 结束条件

结束前会生成报告并持久化记忆。Worker 根据 graph 结果更新 run 和 incident：

- 有执行结果：incident -> `mitigated`。
- 无执行结果：incident -> `resolved`。
- 失败：agent run -> `failed`，记录错误。
- 等待审批：agent run -> `waiting_approval`，incident 保留诊断上下文。
