# LangGraph 工作流设计

## 代码位置

```text
packages/agent/
  graph.py
  runner.py
  state.py
  prompts.py
  schemas.py
  nodes/
    parse_alert.py
    collect_metrics.py
    collect_logs.py
    collect_traces.py
    collect_deployment.py
    retrieve_runbook.py
    diagnose.py
    rank_hypotheses.py
    plan_actions.py
    guardrail_check.py
    human_approval.py
    execute_action.py
    generate_report.py
  guardrails/
    policy.py
    classifier.py
```

## State schema

实现 `packages/agent/state.py`：

```python
class IncidentState(TypedDict, total=False):
    incident_id: str
    agent_run_id: str
    alert_payload: dict
    service_name: str
    severity: str
    alert_name: str
    time_window: dict
    metrics_evidence: list[dict]
    logs_evidence: list[dict]
    traces_evidence: list[dict]
    deployment_evidence: list[dict]
    runbook_context: list[dict]
    memory_context: list[dict]
    hypotheses: list[dict]
    root_cause: dict
    recommended_actions: list[dict]
    approval_status: dict
    execution_result: dict
    incident_report: dict
    token_budget: dict
    compression_events: list[dict]
    errors: list[dict]
```

## Graph 节点

```text
parse_alert
  -> collect_metrics
  -> collect_logs
  -> collect_traces
  -> collect_deployment_context
  -> retrieve_memory
  -> retrieve_runbook
  -> build_context
  -> diagnose
  -> rank_hypotheses
  -> plan_actions
  -> guardrail_check
  -> conditional:
       L0/L1 -> execute_action
       L2/L3 -> human_approval interrupt
       L4    -> generate_report
  -> generate_report
```

## 节点实现规则

每个节点必须遵守：

```python
def node_name(state: IncidentState, deps: AgentDeps) -> IncidentState:
    ...
```

要求：

- 不直接读取环境变量，通过 `deps` 注入。
- 不直接创建数据库 session，通过 service/repository 注入。
- 输入输出要可序列化。
- 大文本不直接进入 state，先写 evidence，再在 state 中保留摘要和 id。
- 每个节点必须记录 `agent_run_nodes`。

## `parse_alert`

输入：`alert_payload`。

输出：`service_name`、`severity`、`alert_name`、`time_window`。

规则：

- `time_window.start = starts_at - 10m`。
- `time_window.end = ends_at or now + 5m`。
- 不调用 LLM。

## `collect_metrics`

调用 MetricsTool，查询：

- error_rate。
- latency。
- qps。
- cpu。
- memory。
- db_connections 或 cache_hit_rate，按 alert type 决定。

输出写入 `metrics_evidence`。

## `collect_logs`

调用 LogsTool，关键词来自：

- alert_name。
- service_name。
- metrics abnormal labels。
- runbook suggested keywords，可在二次查询使用。

日志必须先聚合摘要，不能把原始 100 条日志直接塞入 LLM。

## `collect_traces`

MVP 可使用 mock trace source。输出：慢 span、错误 span、下游依赖。

## `collect_deployment_context`

读取 GitChangeTool。重点判断报警窗口前后 30 分钟是否有变更。

## `retrieve_memory`

调用 MemoryManager：

- incident scope：本次 run 已压缩摘要。
- service scope：服务历史故障摘要。
- procedural scope：诊断策略记忆。

只返回与当前 alert type 和 service 匹配的 top_k 记忆。

## `retrieve_runbook`

调用 RunbookSearchTool。查询由 service、alert_name、metrics/logs 初步证据组成。

## `build_context`

调用 ContextBuilder：

1. 固定 system prompt。
2. 固定 JSON schema instructions。
3. 当前 alert 摘要。
4. 高置信证据。
5. Runbook top chunks。
6. 相关记忆。
7. 压缩后的日志摘要。

必须输出 token 预算统计。

## `diagnose`

LLM 输出 schema：

```python
class DiagnosisOutput(BaseModel):
    hypotheses: list[Hypothesis]
    root_cause: RootCause
    evidence_ids: list[str]
    missing_evidence: list[str]
```

失败处理：

- JSON parse 失败，使用修复 prompt 重试 1 次。
- 仍失败，降级为规则诊断。

## `rank_hypotheses`

排序因子：

- evidence_count。
- evidence_source_diversity。
- deployment correlation。
- runbook match score。
- memory similarity score。

## `plan_actions`

输出 actions。动作必须有：

- `type`
- `target`
- `params`
- `reason`
- `risk_hint`
- `rollback_plan`

## `guardrail_check`

调用 guardrail policy，为每个 action 标注风险等级和状态。

## `human_approval`

L2/L3 生成 approval，并通过 LangGraph interrupt 暂停。

恢复输入：

```python
class ApprovalResumeInput(BaseModel):
    approval_id: str
    action_id: str
    decision: Literal["approved", "rejected"]
    approver: str
    comment: str | None
```

## `execute_action`

只调用 mock executor。执行结果写入 `actions.execution_result`。

## `generate_report`

生成 Markdown report 和结构化 report。报告必须引用 evidence id。
