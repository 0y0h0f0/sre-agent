# 后端架构

**最后更新：** 2026-06-14

## 目标

后端负责把外部告警转换为持久化 incident 和 agent run，把诊断工作安全地交给 Celery worker，并为 React 控制台、审批、runbook、配置、发现和 eval 提供 API。后端不直接执行 LangGraph 诊断图，也不直接绕过 guardrail 执行动作。

## 分层原则

后端采用 `router -> service -> repository -> model` 分层：

```text
HTTP / WebSocket request
  -> Router: path/query/body validation, dependency injection, response model
  -> Service: business rules, transactions, cross-repository orchestration, task enqueue
  -> Repository: SQLAlchemy query/write boundary
  -> Model: packages/db/models.py SQLAlchemy ORM
```

开发约束：

- Router 保持薄层，不承载业务流程、事务编排或数据库查询细节。
- Service 是业务边界，负责状态转换、冲突校验、Celery 入队和审计。
- Repository 是数据库读写入口。新增查询先放 repository，再由 service 调用。
- Pydantic schema 与 SQLAlchemy model 分离。
- 节点/worker 通过 `AgentDeps` 接收依赖，不在 Agent 节点里直接创建 DB session。

## 应用入口

`apps/api/main.py` 中的 `create_app()` 构建 FastAPI app：

- 注册 CORS、GZip、request ID middleware、API key middleware。
- 注册 `AppError` 和 `RequestValidationError` 异常处理器。
- 注册 14 个 HTTP router 和 1 个 WebSocket router。
- 暴露 FastAPI 自动文档 route：`/docs`、`/docs/oauth2-redirect`、`/redoc`、`/openapi.json`。

当前业务 route 口径：76 条 HTTP application route + 1 条 WebSocket。若把 FastAPI 自动文档/OpenAPI route 算入，总 HTTP route 为 80 条。

## Router 层

| Router | 代码 | 业务 route 数 | 职责 |
|--------|------|---------------|------|
| Health | `apps/api/routers/health.py` | 3 | `/healthz`、`/readyz`、`/metrics` |
| Alerts | `apps/api/routers/alerts.py` | 1 | 告警摄取、fingerprint 去重、诊断任务入队 |
| Incidents | `apps/api/routers/incidents.py` | 10 | incident 列表/详情、手动诊断、NFA、反馈、关联、审计 |
| Agent runs | `apps/api/routers/agent_runs.py` | 1 | agent run 详情、节点轨迹、工具调用、token/cache 指标 |
| Reports | `apps/api/routers/reports.py` | 2 | 获取/重新生成版本化 incident report |
| Approvals | `apps/api/routers/approvals.py` | 11 | 审批列表、审批详情、批准/驳回、批量审批、邮件 token |
| Actions | `apps/api/routers/actions.py` | 2 | action 详情、手动 fixture 执行端点 |
| Comments | `apps/api/routers/comments.py` | 5 | incident 评论、证据 annotation |
| Approval groups | `apps/api/routers/approval_groups.py` | 5 | 审批组 CRUD |
| API keys | `apps/api/routers/api_keys.py` | 3 | API key 创建、列表、撤销 |
| Config | `apps/api/routers/config.py` | 8 | EffectiveConfig 发布、回滚、撤销、覆盖项 |
| Discovery | `apps/api/routers/discovery.py` | 6 | 发现状态、服务、指标、拓扑、能力、手动 rerun |
| Runbooks | `apps/api/routers/runbooks.py` | 14 | ingest/search/drafts/templates/versions/M9 LLM/Web/diff |
| Evals | `apps/api/routers/evals.py` | 4 | eval run 创建/列表/详情、shadow run |
| WebSocket | `apps/api/ws/router.py` | 1 WS | `/api/ws/incidents/{incident_id}` 实时事件 |

## Service 层

`apps/api/services/` 当前有 14 个 service 模块：

| Service | 主要职责 |
|---------|----------|
| `alert_service.py` | 创建或去重 incident，创建 agent run，入队诊断任务，NFA 自动降级，通知入队 |
| `incident_service.py` | incident 列表/详情、手动诊断、NFA、反馈、关联、审计读取 |
| `agent_run_service.py` | run 详情、节点、工具调用和 token/cache 展示数据 |
| `report_service.py` | report 获取和重新生成；重新生成创建新版本 |
| `approval_service.py` | L2/L3 审批决策、L3 二次确认、批量审批、resume 入队、审计 |
| `action_service.py` | action 详情和手动 execute 端点；当前手动 execute 使用 fixture executor |
| `comment_service.py` | incident comment 和 evidence annotation |
| `approval_group_service.py` | 审批组 CRUD 与匹配配置 |
| `api_key_service.py` | raw key 生成、SHA-256 hash 存储、验证、撤销、last_used 更新 |
| `runbook_service.py` | runbook ingest/search/draft/review/regenerate/template/M9 draft/diff |
| `eval_service.py` | eval run 创建、列表、详情、shadow run 入队 |
| `email_service.py` | 邮件事件排队、发送、状态和重试记录 |
| `feedback_service.py` | root cause/action/operator feedback |
| `action_service.py` | action 状态和执行响应封装 |

Service 层可以调用多个 repository，并负责提交或回滚事务。Router 不应该直接 `commit()`，除非对应本地模式已经明确把事务封装在 service 内部。

## Repository 层

`packages/db/repositories/` 当前有 26 个 repository 模块。它们封装 SQLAlchemy 查询、状态更新、`SELECT ... FOR UPDATE`、条件筛选和分页。常用边界：

- `incidents.py`：fingerprint 去重、incident 列表、alert payload 重建。
- `agent_runs.py`：run 创建、活动 run 查询、`get_for_update()` 幂等锁、状态转换。
- `actions.py` / `approvals.py`：action/approval 状态、审批列表、approved approval 查询。
- `reports.py`：按 incident 版本化 report。
- `runbooks.py` / `runbook_drafts.py` / `runbook_versions.py`：RAG 和 runbook 生命周期。
- `effective_configs.py` / `discovery_*`：发现、proposal、published config 和 override。
- `audit_logs.py`：append-only 审计记录。

所有 DB 读写应通过 repository，避免在 router 或 Agent node 中写散落 SQL。

## Schema 层

`apps/api/schemas/` 当前有 17 个 schema 模块。公共枚举在 `apps/api/schemas/common.py`：

- `Severity`: `P1`、`P2`、`P3`、`P4`
- `IncidentStatus`: `open`、`diagnosing`、`waiting_approval`、`mitigated`、`resolved`、`failed`
- `AgentRunStatus`: `queued`、`running`、`waiting_approval`、`succeeded`、`failed`、`cancelled`
- `ActionStatus`: `proposed`、`blocked`、`waiting_approval`、`approved`、`rejected`、`executing`、`succeeded`、`failed`
- `ApprovalStatus`: `waiting`、`approved`、`rejected`、`expired`
- `RiskLevel`: `L0`-`L4`

Schema 负责 API 形状和 provider payload 标准化。例如 `AlertCreateRequest` 可接受 unified payload，也可标准化 Alertmanager、PagerDuty、Grafana、Datadog 和 custom payload。

## 依赖注入

`apps/api/dependencies.py` 提供：

| 依赖 | 用途 |
|------|------|
| `get_db()` | SQLAlchemy session generator |
| `get_app_settings()` | cached Settings |
| `get_task_enqueue()` | `run_incident_diagnosis` 入队 helper |
| `get_resume_task_enqueue()` | 审批后恢复 LangGraph 入队 helper |
| `get_notification_task_enqueue()` | 邮件通知入队 helper |
| `get_current_api_key()` | 从 `request.state.api_key` 读取认证身份；auth disabled 时返回 `{}` |
| `require_scope()` / `require_any_scope()` | scope dependency，auth disabled 时跳过检查 |

Scope dependency 当前通过 FastAPI `HTTPException` 返回 401/403，而不是 `AppError`。认证中间件自己的 401 响应使用标准错误信封。

## 核心请求流程

### 告警摄取

```text
POST /api/alerts
  -> AlertCreateRequest 标准化 provider payload
  -> RateLimiter 按 API key ID 或 client IP 限流
  -> AlertService.create_alert()
  -> FalsePositivePatternRepository 判断 NFA 自动降级
  -> IncidentRepository.get_open_by_fingerprint() 去重
  -> 创建 Incident + AgentRun
  -> commit，释放 DB 事务
  -> enqueue_diagnosis_task(incident_id, agent_run_id)
  -> set celery_task_id，commit
  -> 异步通知 new_incident
```

若 Celery 入队失败，service 会把 agent run 标记为 failed，并把 incident 移到 failed，避免后续相同 fingerprint 永远 deduplicate 到一个不会诊断的 open incident。

### 手动诊断

`POST /api/incidents/{incident_id}/diagnose` 创建新的 agent run 并入队。`force=false` 时，如果同一 incident 已有 active run，会返回 409；`force=true` 不删除旧 run，而是创建新 run。

### 审批和恢复

```text
POST /api/approvals/{approval_id}/approve|reject
  -> ApprovalService 锁定业务状态和校验 L3 二次确认
  -> 更新 approval 和 action 状态
  -> 写 audit log
  -> commit 决策，确保 worker 另一个连接可读
  -> 当 run 的审批均已决定后 enqueue_resume_task(agent_run_id, decision)
  -> Worker 使用同一个 LangGraph checkpoint config resume
```

L3 approve 必须提供 `risk_ack=true`、`confirm_action_type == action.type`、`confirm_target == action.target`。

### 手动 action execute

`POST /api/actions/{action_id}/execute` 会重新校验 action 状态和审批记录。当前该 API 端点使用 `FixtureExecutorBackend` 执行，适合受控演示和手动测试。真实 live Kubernetes executor 位于 worker 图执行路径，由 `EXECUTOR_BACKEND=live` 显式选择，并受 guardrail/approval/checkpoint 控制。

## WebSocket 层

`apps/api/ws/router.py` 提供 `WS /api/ws/incidents/{incident_id}`。连接成功后发送 `connected` 消息，随后订阅 Redis Pub/Sub channel `incident:{incident_id}`。Worker node tracer 会通过 `apps/api/ws/publisher.py` 发布节点事件。发布失败只记录 warning，不让 worker 失败。

当 API key auth 启用时，WebSocket 通过查询参数认证：

```text
/api/ws/incidents/{incident_id}?token=<api_key>
```

认证失败关闭连接，code 为 `4001`。

## 事务与幂等

- Celery 任务采用至少一次投递语义，诊断任务通过 `AgentRunRepository.get_for_update()` 和状态检查保证幂等。
- `queued/running/waiting_approval/succeeded/failed` 等状态转换由 repository/service 管理。
- `agent_runs.state` 是展示快照；真实图恢复依赖 LangGraph PostgresSaver checkpoint。
- Report regeneration 创建新版本，不覆盖旧报告。
- API key 原始值只在创建响应中返回一次；数据库只保存 SHA-256 hash。
- Audit log 是 append-only 业务日志；代码不提供更新/删除路径。

## 新增后端能力的落点

| 变更 | 应修改位置 | 必补测试 | 必补文档 |
|------|------------|----------|----------|
| 新 HTTP endpoint | router、schema、service、repository | router/service/unit 或 integration | `api-reference.md` |
| 新 DB 表或字段 | `packages/db/models.py`、migration、repository | migration/repository/integration | `data-model.md` |
| 新 Celery task | `apps/worker/tasks.py` 或专用 task 模块 | eager/idempotency/retry tests | `celery-and-jobs.md` |
| 新 protected scope | dependency/router/API key docs | auth/scope tests | `auth-and-api-keys.md`、`api-reference.md` |
| 新错误类型 | `packages/common/errors.py` 或 exception handler | error envelope tests | `errors-and-request-ids.md` |
| 新 read/write safety-sensitive path | service/tool/guardrail | safety boundary tests | scope/boundary/API docs |
