# API 参考

**最后更新：** 2026-06-14

## 口径

当前 FastAPI app 暴露 80 条 HTTP route，其中 4 条为 FastAPI 自动文档/OpenAPI route：`/docs`、`/docs/oauth2-redirect`、`/redoc`、`/openapi.json`。本文统计业务 API 为 76 条 HTTP route，另有 1 条 WebSocket route。

## 通用约定

- 请求和响应体为 JSON，除 `/metrics`、邮件 token HTML 页面和 WebSocket 外。
- 时间戳使用带时区 UTC ISO 8601。
- 分页默认使用 `page`（从 1 开始）和 `page_size`（默认 20，通常最大 100，具体以 schema/query 限制为准）。
- 客户端可发送 `X-Request-Id`；缺失时服务端生成 `req_` 前缀 ID，并在响应头返回。
- 业务错误应使用标准错误信封；FastAPI `HTTPException` 仍可能返回默认 `{"detail": ...}` 结构，详见 [错误响应与请求 ID](errors-and-request-ids.md)。
- API 绝不在请求线程中运行 LangGraph 诊断图；诊断通过 Celery 入队。

标准错误信封：

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

API key middleware 由 `API_KEY_AUTH_ENABLED` 控制。`packages/common/settings.py` 默认启用；`docker-compose.yml` 为本地 demo 将 API/worker 环境中的 `API_KEY_AUTH_ENABLED` 设为 false。

启用认证时，开放路径来自 `API_KEY_OPEN_PATHS`，默认包含：

```text
/healthz,/readyz,/metrics,/docs,/openapi.json,/api/approvals/by-token
```

开放路径使用边界感知前缀匹配，因此 `/docs/oauth2-redirect` 也开放。`/redoc` 当前不在默认开放路径中。

受保护 HTTP endpoint 使用：

```text
Authorization: Bearer <api_key>
```

WebSocket 先用普通 bearer API key 申请短期 ticket，再连接：

```text
POST /api/ws/incidents/{incident_id}/ticket
/api/ws/incidents/{incident_id}?ticket=<short_lived_ticket>
```

Scope enforcement 当前用于 API key 管理、配置、发现和 M9 runbook 外部/LLM 能力。API key 管理 endpoints 需要 `api_key:admin` scope。

## Route 总览

| 分组 | Route 数 | 入口 |
|------|----------|------|
| Health | 3 | `/healthz`、`/readyz`、`/metrics` |
| Alerts | 1 | `/api/alerts` |
| Incidents / collaboration / reports | 18 | `/api/incidents`、`/api/evidence`、`/api/comments` |
| Agent runs | 1 | `/api/agent-runs` |
| Approvals | 11 | `/api/approvals` |
| Actions | 2 | `/api/actions` |
| Approval groups | 5 | `/api/approval-groups` |
| API keys | 3 | `/api/api-keys` |
| Config | 8 | `/api/config` |
| Discovery | 6 | `/api/discovery` |
| Runbooks | 14 | `/api/runbooks` |
| Evals | 4 | `/api/evals` |
| WebSocket | 1 | `/api/ws/incidents/{incident_id}` |

## Health

| 方法 | 路径 | 描述 |
|------|------|------|
| GET | `/healthz` | 存活探针，不检查依赖。 |
| GET | `/readyz` | 就绪探针，检查 PostgreSQL、Redis、Celery broker。 |
| GET | `/metrics` | Prometheus text metrics endpoint。 |

## Alerts

| 方法 | 路径 | 描述 |
|------|------|------|
| POST | `/api/alerts` | 摄取告警，创建或去重 incident，创建 agent run，并入队诊断任务。HTTP 202。 |

`AlertCreateRequest` 支持 unified payload，也能标准化 Alertmanager、PagerDuty、Grafana、Datadog 和 custom payload。核心字段：`source`、`fingerprint`、`service`、`severity`、`alert_name`、`starts_at`、`ends_at`、`labels`、`annotations`、`raw_payload`。

示例：

```json
{
  "source": "mock",
  "fingerprint": "checkout-api-high-5xx",
  "service": "checkout-api",
  "severity": "P2",
  "alert_name": "High5xxAfterDeploy",
  "starts_at": "2026-06-04T00:00:00Z",
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

同一 fingerprint 的未终态 incident 会 deduplicate，响应返回既有 incident/latest run 信息。告警 endpoint 受 Redis sliding-window rate limit 保护；Redis 不可用时故障开放。

## Incidents、Reports、Collaboration

| 方法 | 路径 | 描述 |
|------|------|------|
| GET | `/api/incidents` | 列出 incident。支持 `status`、`service`、`severity`、分页。 |
| GET | `/api/incidents/{incident_id}` | incident 详情，包含根因、证据、建议动作。 |
| GET | `/api/incidents/{incident_id}/runs` | incident 的 agent run 列表。 |
| POST | `/api/incidents/{incident_id}/diagnose` | 手动触发诊断。`force=false` 时已有 active run 返回 409。HTTP 202。 |
| POST | `/api/incidents/{incident_id}/nfa` | 标记 Not Actionable Alert。HTTP 201。 |
| PATCH | `/api/incidents/{incident_id}/root-cause` | 纠正根因并记录反馈。 |
| POST | `/api/incidents/{incident_id}/actions/{action_id}/feedback` | 提交 action 反馈。 |
| GET | `/api/incidents/{incident_id}/correlated` | 获取跨 incident 关联。 |
| GET | `/api/incidents/{incident_id}/feedback` | 获取 incident 反馈列表。 |
| GET | `/api/incidents/{incident_id}/audit` | 获取 incident 审计日志。 |
| GET | `/api/incidents/{incident_id}/report` | 获取最新 incident report。 |
| POST | `/api/incidents/{incident_id}/report/regenerate` | 重新生成 report，创建新版本，不覆盖旧版本。HTTP 201。 |
| POST | `/api/incidents/{incident_id}/comments` | 创建 incident comment。HTTP 201。 |
| GET | `/api/incidents/{incident_id}/comments` | 列出 incident comment。 |
| DELETE | `/api/comments/{comment_id}` | 删除 comment。HTTP 204。 |
| POST | `/api/evidence/{evidence_id}/annotations` | 创建 evidence annotation。HTTP 201。 |
| GET | `/api/evidence/{evidence_id}/annotations` | 列出 evidence annotation。 |

手动诊断请求：

```json
{
  "force": false,
  "reason": "manual retry after runbook ingest"
}
```

Report 字段包括 `report_id`、`incident_id`、`agent_run_id`、`version`、`root_cause`、`impact`、`timeline`、`actions`、`follow_ups`、`body_markdown`、`created_at`。

## Agent Runs

| 方法 | 路径 | 描述 |
|------|------|------|
| GET | `/api/agent-runs/{agent_run_id}` | 获取 run 详情、节点轨迹、工具调用、token/cache 指标和 checkpoint pointer。 |

响应包含 `checkpoint_thread_id`、`checkpoint_ns`、`latest_checkpoint_id`。这些字段是业务表中的 checkpoint pointer；`agent_runs.state` 只是展示快照。

## Approvals

| 方法 | 路径 | 描述 |
|------|------|------|
| GET | `/api/approvals` | 列出审批。支持 `status`、`incident_id`、`service`、`risk_level`、分页。 |
| GET | `/api/approvals/{approval_id}` | 审批详情。 |
| GET | `/api/incidents/{incident_id}/approvals` | incident 的审批列表。 |
| POST | `/api/approvals/{approval_id}/approve` | 批准审批。L3 需要二次确认字段。 |
| POST | `/api/approvals/{approval_id}/reject` | 拒绝审批。 |
| POST | `/api/approvals/batch` | 批量审批，最多 50 个。 |
| POST | `/api/approvals/{approval_id}/email-token` | 生成邮件审批 token。 |
| GET | `/api/approvals/by-token/{token}` | 邮件 token 入口，重定向到前端审批页面。 |
| GET | `/api/approvals/by-token/{token}/approve` | 邮件批准 HTML 确认页。 |
| POST | `/api/approvals/by-token/{token}/approve` | 通过邮件 token 批准。 |
| GET | `/api/approvals/by-token/{token}/reject` | 邮件拒绝 HTML 确认页。 |
| POST | `/api/approvals/by-token/{token}/reject` | 通过邮件 token 拒绝。 |

L2 approve 示例：

```json
{
  "approver": "alice",
  "comment": "approved for demo"
}
```

L3 approve 示例：

```json
{
  "approver": "alice",
  "comment": "rollback approved",
  "risk_ack": true,
  "confirm_action_type": "rollback_release",
  "confirm_target": "checkout-api"
}
```

审批 service 会先提交决策，再入队 `resume_incident_after_approval`，确保 worker 的独立 DB 连接能读到审批状态。

## Actions

| 方法 | 路径 | 描述 |
|------|------|------|
| GET | `/api/actions/{action_id}` | 获取 action 详情。 |
| POST | `/api/actions/{action_id}/execute` | 手动执行已批准 action。当前实现使用 fixture executor。 |

执行请求：

```json
{
  "operator": "alice",
  "reason": "manual approved action"
}
```

该端点会重新验证 action 状态、L2/L3 审批和 L3 二次确认字段。L4 永远不可执行。真实 live K8s executor 不从该手动 API 直接触发；它由 worker 图执行路径按 `EXECUTOR_BACKEND=live` 选择。

## Approval Groups

| 方法 | 路径 | 描述 |
|------|------|------|
| GET | `/api/approval-groups` | 列出审批组。 |
| POST | `/api/approval-groups` | 创建审批组。HTTP 201。 |
| GET | `/api/approval-groups/{group_id}` | 获取审批组。 |
| PATCH | `/api/approval-groups/{group_id}` | 更新审批组。 |
| DELETE | `/api/approval-groups/{group_id}` | 删除审批组。HTTP 204。 |

审批组使用 `service_pattern` 匹配 incident 服务，`members` 存储审批人列表。

## API Keys

| 方法 | 路径 | 描述 |
|------|------|------|
| GET | `/api/api-keys` | 列出 API key 元数据，不返回 raw key。 |
| POST | `/api/api-keys` | 创建 API key；raw key 只在响应中返回一次。HTTP 201。 |
| DELETE | `/api/api-keys/{key_id}` | 撤销 API key。HTTP 204。 |

创建请求当前只接受：

```json
{
  "description": "production-operator-key",
  "expires_in_days": 90
}
```

创建响应包含 `raw_key`；数据库只保存 SHA-256 hash。

## Config

读取 endpoint 需要 `config:read` 或 `config:write`；写 endpoint 需要 `config:write`。

| 方法 | 路径 | Scope | 描述 |
|------|------|-------|------|
| GET | `/api/config/current` | `config:read` 或 `config:write` | 当前 active published config；无发布版本时返回 `status: none`。 |
| GET | `/api/config/versions` | `config:read` 或 `config:write` | 最近配置版本，`limit` 默认 10，最大 100。 |
| POST | `/api/config/publish` | `config:write` | 发布新 `EffectiveConfigVersion`，取代旧 published 版本。HTTP 201。 |
| POST | `/api/config/rollback` | `config:write` | 回滚到之前的 published 版本。 |
| POST | `/api/config/revoke` | `config:write` | 撤销配置版本，使 worker 不再选择它。 |
| GET | `/api/config/overrides` | `config:read` 或 `config:write` | 列出 active overrides。 |
| POST | `/api/config/overrides` | `config:write` | 创建 override。HTTP 201。 |
| DELETE | `/api/config/overrides/{override_id}` | `config:write` | 撤销 override，保留审计记录。 |

Override 规则：`reason` 必填；默认 TTL backend URL 为 7 天，其他为 14 天；最大 TTL 30 天；禁止在 override 中设置 secret/auth/executor/live 字段；URL override 会经过 backend URL safety validator。

配置优先级：

```text
env > active override > profile > published EffectiveConfigVersion > safe default
```

Worker 仅读取 published config，不读取未发布 proposal。

## Discovery

读取 endpoint 需要 `discovery:read` 或 `discovery:write`；rerun 需要 `discovery:write`。

| 方法 | 路径 | Scope | 描述 |
|------|------|-------|------|
| GET | `/api/discovery/status` | `discovery:read` 或 `discovery:write` | 发现系统状态和最近运行历史。 |
| GET | `/api/discovery/services` | `discovery:read` 或 `discovery:write` | 最近成功发现的服务。 |
| GET | `/api/discovery/metrics` | `discovery:read` 或 `discovery:write` | 指标映射、PromQL 模板、置信度和状态。 |
| GET | `/api/discovery/topology` | `discovery:read` 或 `discovery:write` | workload binding 和服务间连接。 |
| GET | `/api/discovery/capabilities` | `discovery:read` 或 `discovery:write` | 服务能力矩阵。 |
| POST | `/api/discovery/rerun` | `discovery:write` | 手动触发发现重新运行。HTTP 202。 |

若已有发现运行持有锁，rerun 返回 `locked`，不会重复入队。

## Runbooks

| 方法 | 路径 | Scope | 描述 |
|------|------|-------|------|
| POST | `/api/runbooks/ingest` | 全局 auth | 从目录摄取 runbook。 |
| GET | `/api/runbooks/search` | 全局 auth | 搜索 runbook chunks；`q` 必填，`top_k` 1-20。 |
| GET | `/api/runbooks/drafts` | 全局 auth | 列出 runbook drafts。 |
| POST | `/api/runbooks/drafts/generate` | 全局 auth | 从 incident cluster 生成确定性 draft。 |
| GET | `/api/runbooks/drafts/{draft_id}` | 全局 auth | draft 详情。 |
| POST | `/api/runbooks/drafts/{draft_id}/review` | 全局 auth | 审核 draft。 |
| POST | `/api/runbooks/drafts/{draft_id}/regenerate` | 全局 auth | 重新生成 draft，创建新版本。 |
| POST | `/api/runbooks/template` | 全局 auth | 生成模板 draft。 |
| POST | `/api/runbooks/llm-generate` | `runbook:review` 或 `runbook:llm_generate` | M9 LLM runbook draft。只生成待审草稿。 |
| POST | `/api/runbooks/web-search` | `runbook:review` 且 `runbook:web_search` | M9 web search enrichment。 |
| POST | `/api/runbooks/incident-diff` | `runbook:review` 且 `incident:llm_diff`；外部 LLM 还需 `llm:invoke` 或 `ai:external` | M9 incident/runbook diff，生成 amendment draft。 |
| GET | `/api/runbooks/amendments` | 全局 auth | 列出 amendment drafts。 |
| POST | `/api/runbooks/amendments/{amendment_id}/review` | 全局 auth | 审核 amendment。 |
| GET | `/api/runbooks/versions/{document_id}` | 全局 auth | 列出 runbook document versions。 |

Search 响应项包含 `chunk_id`、`source_path`、`title`、`excerpt`、`score`、`metadata`。

M9 endpoints 同时受 `M9_EXTENSIONS_ENABLED` 和对应子开关控制，关闭时返回 `disabled`、`blocked` 或 `degraded` 状态，不自动发布或执行。

## Evals

| 方法 | 路径 | 描述 |
|------|------|------|
| GET | `/api/evals/runs` | 列出 eval runs。 |
| POST | `/api/evals/runs` | 创建 eval run 并入队。HTTP 201。 |
| GET | `/api/evals/runs/{eval_run_id}` | eval run 详情。 |
| POST | `/api/evals/shadow` | 为 incident 触发 shadow eval。HTTP 201。 |

CI smoke eval 必须使用 FakeLLM。真实 LLM 只允许 manual full eval 或手动 demo。

## WebSocket

| 类型 | 路径 | 描述 |
|------|------|------|
| WS | `/api/ws/incidents/{incident_id}` | 订阅 incident 节点事件。 |

连接成功消息：

```json
{
  "type": "connected",
  "incident_id": "inc_xxx"
}
```

之后消息来自 Redis Pub/Sub channel `incident:{incident_id}`，由 worker node tracer 发布。

## 枚举

| 枚举 | 值 |
|------|----|
| `Severity` | `P1`、`P2`、`P3`、`P4` |
| `IncidentStatus` | `open`、`diagnosing`、`waiting_approval`、`mitigated`、`resolved`、`failed` |
| `AgentRunStatus` | `queued`、`running`、`waiting_approval`、`succeeded`、`failed`、`cancelled` |
| `RiskLevel` | `L0`、`L1`、`L2`、`L3`、`L4` |
| `ActionStatus` | `proposed`、`blocked`、`waiting_approval`、`approved`、`rejected`、`executing`、`succeeded`、`failed` |
| `ApprovalStatus` | `waiting`、`approved`、`rejected`、`expired` |
| `AlertSource` | `alertmanager`、`pagerduty`、`grafana`、`datadog`、`custom`、`mock` |

## Feature Flags

关键 M9 / backend flags 见 [配置参考](../11-reference/configuration.md)。API 侧最常见影响：

- `M9_EXTENSIONS_ENABLED=false` 强制关闭 M9 子能力。
- `RUNBOOK_LLM_GENERATION_ENABLED` 控制 LLM runbook draft。
- `LLM_INCIDENT_DIFF_ENABLED` 控制 incident diff。
- `RUNBOOK_WEB_SEARCH_ENABLED` 控制 web search。
- `SEMANTIC_RUNBOOK_SEARCH_ENABLED` 和 `EXTERNAL_EMBEDDING_PROVIDER_ENABLED` 控制语义搜索/外部 embedding。
- `TRACE_ENABLED` / `TRACE_BACKEND` 控制 trace 后端。
- `GRAFANA_ALERT_INGEST_ENABLED` 当前影响 alert service 的 Grafana payload ingest 能力；业务路由仍是 `/api/alerts`。
