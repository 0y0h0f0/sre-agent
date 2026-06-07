# API 契约

## 通用约定

- 请求和响应均使用 JSON。
- 写接口必须接收 `X-Request-Id`，没有则服务端生成；服务端必须在响应 header 和错误体中返回最终 `request_id`。
- 时间使用 UTC ISO 8601。
- 分页参数：`page` 默认 1，`page_size` 默认 20，最大 100。
- 错误响应统一为 `{ error: { code, message, request_id, details } }`。

## 状态枚举

```python
IncidentStatus = Literal["open", "diagnosing", "waiting_approval", "mitigated", "resolved", "failed"]
Severity = Literal["P1", "P2", "P3", "P4"]
AgentRunStatus = Literal["queued", "running", "waiting_approval", "succeeded", "failed", "cancelled"]
RiskLevel = Literal["L0", "L1", "L2", "L3", "L4"]
ActionStatus = Literal["proposed", "blocked", "waiting_approval", "approved", "rejected", "executing", "succeeded", "failed"]
```

## Health

### `GET /healthz`

返回进程存活状态，不访问外部依赖。

```json
{"status": "ok"}
```

### `GET /readyz`

检查 PostgreSQL、Redis、Celery broker。

```json
{
  "status": "ready",
  "dependencies": {
    "postgres": "ok",
    "redis": "ok",
    "celery_broker": "ok"
  }
}
```

## Alerts

### `POST /api/alerts`

创建 incident 并入队诊断任务。

请求 schema：

```python
class AlertCreateRequest(BaseModel):
    source: Literal["alertmanager", "mock"]
    fingerprint: str
    service: str
    severity: Severity
    alert_name: str
    starts_at: datetime
    ends_at: datetime | None = None
    labels: dict[str, str] = Field(default_factory=dict)
    annotations: dict[str, str] = Field(default_factory=dict)
```

响应 schema：

```python
class AlertCreateResponse(BaseModel):
    incident_id: str
    agent_run_id: str
    celery_task_id: str
    status: Literal["queued"]
    deduplicated: bool
```

实现要求：

- `fingerprint` 在未关闭 incident 中唯一。
- 重复 fingerprint 返回已有 incident，不新建。
- API 不直接执行 LangGraph。
- Celery 入队失败要记录到 `agent_runs.error_message`。

## Incidents

### `GET /api/incidents`

查询参数：`status`、`service`、`severity`、`page`、`page_size`。

响应 item：

```python
class IncidentListItem(BaseModel):
    incident_id: str
    service: str
    severity: Severity
    status: IncidentStatus
    alert_name: str
    root_cause_summary: str | None
    created_at: datetime
    updated_at: datetime
```

### `GET /api/incidents/{incident_id}`

响应 schema：

```python
class IncidentDetailResponse(BaseModel):
    incident_id: str
    service: str
    severity: Severity
    status: IncidentStatus
    alert: dict
    root_cause: RootCause | None
    evidence: list[EvidenceItem]
    recommended_actions: list[ActionSummary]
```

### `POST /api/incidents/{incident_id}/diagnose`

手动触发诊断。

```python
class DiagnoseRequest(BaseModel):
    force: bool = False
    reason: str | None = None
```

规则：

- `force=false` 时，已有 running run 则返回 409。
- `force=true` 时，创建新的 agent_run，但不能删除旧 run。

## Agent Runs

### `GET /api/incidents/{incident_id}/runs`

返回某 incident 的 run 列表。

### `GET /api/agent-runs/{agent_run_id}`

返回节点轨迹：

```python
class AgentRunNode(BaseModel):
    name: str
    status: str
    started_at: datetime | None
    finished_at: datetime | None
    duration_ms: int | None
    input_summary: str | None
    output_summary: str | None
    tool_calls: list[str]
```

## Approvals

### `GET /api/approvals`

用途：查询待审批或历史审批记录，供 React 审批页使用。

查询参数：

- `status`：可选，`waiting`、`approved`、`rejected`、`expired`。
- `incident_id`：可选。
- `service`：可选。
- `risk_level`：可选，通常筛选 `L2` 或 `L3`。
- `page`、`page_size`：分页。

响应 item：

```python
class ApprovalListItem(BaseModel):
    approval_id: str
    action_id: str
    incident_id: str
    agent_run_id: str
    service: str
    action_type: str
    risk_level: RiskLevel
    approval_status: Literal["waiting", "approved", "rejected", "expired"]
    action_status: ActionStatus
    reason: str
    rollback_plan: str | None
    requested_at: datetime
```

### `GET /api/incidents/{incident_id}/approvals`

用途：查询单个 incident 下的审批记录。返回 schema 与 `GET /api/approvals` 相同。

### `POST /api/approvals/{approval_id}/approve`

```python
class ApprovalDecisionRequest(BaseModel):
    approver: str
    comment: str | None = None
    risk_ack: bool = False
    confirm_action_type: str | None = None
    confirm_target: str | None = None
```

规则：

- 只有 `waiting` approval 可以 approve。
- L2 只要求 `approver`，`comment` 可为空。
- L3 必须满足 `risk_ack=true`、`confirm_action_type == action.type`、`confirm_target == action.target`，否则返回 400。
- 审批通过后使用 LangGraph checkpoint 恢复，配置为 `thread_id=agent_run_id`、`checkpoint_ns=""`。
- 重复 approve 返回 409，并写测试覆盖。

### `POST /api/approvals/{approval_id}/reject`

请求 schema 同 approve，但 reject 不要求 L3 二次确认字段。

拒绝后 LangGraph 生成替代建议或直接结束。

## Actions

### `GET /api/actions/{action_id}`

返回动作、风险、参数、状态和回滚计划。

### `POST /api/actions/{action_id}/execute`

只允许执行已批准的 L2/L3 或自动放行的 L0/L1。MVP 调用 mock executor。

执行前校验：

- L0/L1：`status` 必须是 `approved` 或由 guardrail 标记为自动放行。
- L2：必须存在 `approved` approval。
- L3：必须存在 `approved` approval，且 approval 中保存了 `risk_ack=true`、`confirm_action_type`、`confirm_target`。
- L4：永远不能执行。

## Runbooks

### `POST /api/runbooks/ingest`

```python
class RunbookIngestRequest(BaseModel):
    path: str = "demo/runbooks"
    reingest: bool = True
```

### `GET /api/runbooks/search`

参数：`q`、`service`、`incident_type`、`top_k`。

返回必须包含 `chunk_id`、`score`、`source_path`、`title`、`excerpt`、`metadata`。

## Reports

### `GET /api/incidents/{incident_id}/report`

返回复盘报告。

### `POST /api/incidents/{incident_id}/report/regenerate`

重新生成报告必须创建新的 report 版本，`version = previous_version + 1`；不得覆盖旧版本。
