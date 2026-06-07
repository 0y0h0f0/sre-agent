# API 参考

## 通用约定

- 请求和响应使用 JSON。
- 时间使用 timezone-aware UTC ISO 8601。
- 分页参数通常为 `page` 和 `page_size`，`page_size` 最大 100。
- 写接口应传入 `X-Request-Id`；缺失时服务端生成。
- 错误响应统一为：

```json
{
  "error": {
    "code": "VALIDATION_ERROR",
    "message": "request validation failed",
    "request_id": "req_xxx",
    "details": {}
  }
}
```

## 认证

默认启用 API key 鉴权。除开放路径外，HTTP API 需要：

```text
Authorization: Bearer <api_key>
```

WebSocket 在鉴权启用时使用 query token：

```text
/api/ws/incidents/{incident_id}?token=<api_key>
```

## Health

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/healthz` | 进程存活检查，不访问外部依赖 |
| GET | `/readyz` | PostgreSQL、Redis、Celery broker readiness |
| GET | `/metrics` | Prometheus metrics |

## Alerts

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/alerts` | 创建或复用 incident，创建 agent run，入队诊断 |

`AlertCreateRequest` 支持统一 payload，也支持 Alertmanager、PagerDuty、Grafana、Datadog、custom 和 mock payload 归一化。核心字段：

```json
{
  "source": "mock",
  "fingerprint": "checkout-api-high-5xx",
  "service": "checkout-api",
  "severity": "P2",
  "alert_name": "High5xxAfterDeploy",
  "starts_at": "2026-06-04T00:00:00Z",
  "ends_at": null,
  "labels": {},
  "annotations": {},
  "raw_payload": {}
}
```

响应：

```json
{
  "incident_id": "inc_xxx",
  "agent_run_id": "run_xxx",
  "celery_task_id": "celery-id",
  "status": "queued",
  "deduplicated": false
}
```

## Incidents

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/incidents` | 分页查询事故，支持 `status`、`service`、`severity` |
| GET | `/api/incidents/{incident_id}` | 查询事故详情、根因、证据、动作 |
| GET | `/api/incidents/{incident_id}/runs` | 查询事故关联的 agent run |
| POST | `/api/incidents/{incident_id}/diagnose` | 手动触发诊断 |
| POST | `/api/incidents/{incident_id}/nfa` | 标记 Not Actionable Alert |
| PATCH | `/api/incidents/{incident_id}/root-cause` | 修正根因并记录反馈 |
| POST | `/api/incidents/{incident_id}/actions/{action_id}/feedback` | 修正动作反馈 |
| GET | `/api/incidents/{incident_id}/correlated` | 查询跨事故关联 |
| GET | `/api/incidents/{incident_id}/feedback` | 查询事故反馈 |
| GET | `/api/incidents/{incident_id}/audit` | 查询事故审计日志 |

手动诊断请求：

```json
{
  "force": false,
  "reason": "manual retry after runbook ingest"
}
```

`force=false` 时，如果已有 running run，应返回 409。`force=true` 创建新 run，但不删除旧 run。

## Agent Runs

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/agent-runs/{agent_run_id}` | 查询 run 详情、节点轨迹、token/cache 指标 |

Agent run 页面需要展示：

- status、started/finished/duration。
- model、prompt version。
- node traces。
- tool calls。
- prompt/completion token。
- provider cache hit/miss。
- app cache hit/miss。
- compression events。

## Approvals

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/approvals` | 分页查询审批，支持 `status`、`incident_id`、`service`、`risk_level` |
| GET | `/api/approvals/{approval_id}` | 查询单个审批 |
| GET | `/api/incidents/{incident_id}/approvals` | 查询事故下审批 |
| POST | `/api/approvals/{approval_id}/approve` | 审批通过 |
| POST | `/api/approvals/{approval_id}/reject` | 拒绝审批 |
| POST | `/api/approvals/batch` | 批量 approve/reject |
| POST | `/api/approvals/{approval_id}/email-token` | 生成邮件审批 token |
| GET | `/api/approvals/by-token/{token}` | 邮件 token 跳转到前端审批页 |
| POST | `/api/approvals/by-token/{token}/approve` | 通过邮件 token 审批 |
| POST | `/api/approvals/by-token/{token}/reject` | 通过邮件 token 拒绝 |

L2 approve：

```json
{
  "approver": "alice",
  "comment": "approved for demo"
}
```

L3 approve：

```json
{
  "approver": "alice",
  "comment": "rollback approved",
  "risk_ack": true,
  "confirm_action_type": "rollback_release",
  "confirm_target": "checkout-api"
}
```

## Actions

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/actions/{action_id}` | 查询动作详情 |
| POST | `/api/actions/{action_id}/execute` | 执行动作，MVP 使用 mock executor |

执行请求：

```json
{
  "operator": "alice",
  "reason": "manual approved action"
}
```

执行前必须重新校验 action 状态和审批记录。L4 永远不能执行。

## Runbooks

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/runbooks/ingest` | 从目录入库 Runbook |
| GET | `/api/runbooks/search` | 搜索 Runbook chunk |
| GET | `/api/runbooks/drafts` | 查询 Runbook 草稿 |
| GET | `/api/runbooks/drafts/{draft_id}` | 查询单个草稿 |
| POST | `/api/runbooks/drafts/generate` | 生成 Runbook 草稿 |
| POST | `/api/runbooks/drafts/{draft_id}/review` | 审核草稿 |
| GET | `/api/runbooks/versions/{document_id}` | 查询 Runbook 版本 |

入库请求：

```json
{
  "path": "demo/runbooks",
  "reingest": true
}
```

搜索参数：

```text
q=<query>&service=checkout-api&incident_type=high-5xx&top_k=5
```

搜索响应必须包含 `chunk_id`、`source_path`、`title`、`excerpt`、`score`、`metadata`。

## Reports

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/incidents/{incident_id}/report` | 查询最新事故报告 |
| POST | `/api/incidents/{incident_id}/report/regenerate` | 重新生成报告，创建新版本 |

报告版本不得覆盖旧版本，`(incident_id, version)` 唯一。

## Collaboration

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/incidents/{incident_id}/comments` | 创建事故评论 |
| GET | `/api/incidents/{incident_id}/comments` | 查询事故评论 |
| DELETE | `/api/comments/{comment_id}` | 删除评论 |
| POST | `/api/evidence/{evidence_id}/annotations` | 创建证据标注 |
| GET | `/api/evidence/{evidence_id}/annotations` | 查询证据标注 |

## Approval Groups

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/approval-groups` | 创建审批组 |
| GET | `/api/approval-groups` | 查询审批组 |
| GET | `/api/approval-groups/{group_id}` | 查询单个审批组 |
| PATCH | `/api/approval-groups/{group_id}` | 更新审批组 |
| DELETE | `/api/approval-groups/{group_id}` | 删除审批组 |

审批组按 service pattern 匹配，用于审批通知路由。

## API Keys

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/api-keys` | 创建 API key，raw key 只返回一次 |
| GET | `/api/api-keys` | 查询 key metadata |
| DELETE | `/api/api-keys/{key_id}` | 撤销 key |

## Evals

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/evals/runs` | 触发 eval run |
| GET | `/api/evals/runs` | 查询 eval run 列表 |
| GET | `/api/evals/runs/{eval_run_id}` | 查询 eval run 详情 |
| POST | `/api/evals/shadow` | 触发 shadow mode run |

## WebSocket

| 类型 | 路径 | 说明 |
| --- | --- | --- |
| WS | `/api/ws/incidents/{incident_id}` | 订阅事故诊断节点事件 |

连接成功后服务端发送：

```json
{
  "type": "connected",
  "incident_id": "inc_xxx"
}
```

Worker 通过 Redis pub/sub 发布节点事件。
