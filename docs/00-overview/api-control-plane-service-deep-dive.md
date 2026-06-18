# API 控制面与服务层技术深挖

**最后更新：** 2026-06-17

本文沿当前代码路径说明 FastAPI 控制面如何处理 HTTP 请求、认证、scope、request id、错误信封、rate limit、service 事务、Celery 入队、审计和安全边界。它补充 [后端架构](../01-backend/backend-architecture.md)、[API 参考](../01-backend/api-reference.md)、[错误响应与请求 ID](../01-backend/errors-and-request-ids.md) 和 [API Key 鉴权](../01-backend/auth-and-api-keys.md)：这些文档分别列出结构和端点，本文解释一次真实请求如何穿过控制面。需要进一步聚焦 API key、scope、bootstrap、WebSocket ticket、audit 和 redaction 时，见 [认证、API Key、审计与安全边界技术深挖](auth-api-key-audit-security-deep-dive.md)。

## 阅读目标

读完本文应能回答：

- `create_app()` 注册了哪些 middleware、exception handler、router。
- API key middleware、scope dependency 和 open path 的职责边界是什么。
- 哪些错误会进入标准 `{error:{...}}` 信封，哪些响应不是 JSON 信封。
- 告警摄取、手动诊断、审批、报告、API key、config/discovery 的事务提交点在哪里。
- 为什么需要先提交 incident/run/approval，再入队 Celery 或 resume worker。
- 哪些业务写路径会写 audit log，哪些是 best-effort 通知或实时增强。
- 新增 API 时应该把代码放在 router、schema、service、repository 的哪一层。

## 代码入口

| 主题 | 当前入口 |
|------|----------|
| FastAPI app factory | `apps/api/main.py` |
| Dependency injection | `apps/api/dependencies.py` |
| API key middleware | `apps/api/middleware/auth.py` |
| Rate limiter | `apps/api/rate_limit.py` |
| HTTP routers | `apps/api/routers/*.py` |
| Pydantic schemas | `apps/api/schemas/*.py` |
| Service layer | `apps/api/services/*.py` |
| DB repositories | `packages/db/repositories/*.py` |
| Common errors | `packages/common/errors.py` |
| API key repository/service | `packages/db/repositories/api_keys.py`、`apps/api/services/api_key_service.py` |
| Audit repository | `packages/db/repositories/audit_logs.py` |
| Worker enqueue helpers | `apps/worker/tasks.py` |
| WebSocket realtime | `apps/api/ws/router.py`、`apps/api/ws/publisher.py` |

## 总链路

```text
HTTP request
  -> FastAPI middleware
       -> request id
       -> API key authentication
       -> GZip / CORS where applicable
  -> Router
       -> path/query/body validation
       -> dependency injection
       -> response_model
  -> Service
       -> business rules
       -> repository orchestration
       -> transaction commit/rollback
       -> task enqueue or audit where needed
  -> Repository
       -> SQLAlchemy query/write boundary
  -> Response
       -> Pydantic response model
       -> X-Request-Id header
       -> standard error envelope on handled errors
```

API 控制面不运行 LangGraph。所有诊断执行和 approval resume 都通过 Celery worker 进入 LangGraph；执行面细节见 [Worker、Celery 与 LangGraph Checkpoint 技术深挖](worker-celery-langgraph-checkpoint-deep-dive.md)。

## 1. App Bootstrap

`apps/api/main.py:create_app()` 当前做这些注册：

| 类型 | 当前实现 |
|------|----------|
| App metadata | `FastAPI(title="SRE Incident Response Agent", version="0.1.0")` |
| CORS | 当 `CORS_ALLOW_ORIGINS` 非空时注册 `CORSMiddleware`。 |
| Compression | `GZipMiddleware(minimum_size=1000)`。 |
| Request ID | `_request_id_middleware` 读取或生成 `req_` ID，并写响应头。 |
| API key auth | `create_api_key_middleware()`。 |
| Error handlers | `AppError`、`HTTPException`、`RequestValidationError`。 |
| Routers | 14 个 HTTP router + WebSocket router。 |

业务 route 口径仍是 79 条 HTTP route + 1 条 WebSocket route。FastAPI 自动文档 route 另有 `/docs`、`/docs/oauth2-redirect`、`/redoc`、`/openapi.json`。

## 2. Request ID and Error Envelope

请求 ID 规则：

- 如果客户端传入 `X-Request-Id`，沿用它。
- 如果缺失，服务端用 `new_id("req_")` 生成。
- 写入 `request.state.request_id`。
- 响应头返回同一个 `X-Request-Id`。

当前三个 exception handler 都返回标准错误信封：

| 异常 | Handler | HTTP / code |
|------|---------|-------------|
| `AppError` | `_app_error_handler` | 使用异常自带 `status_code` 和 `code`。 |
| `HTTPException` | `_http_exception_handler` | 按 status 映射 `VALIDATION_ERROR`、`UNAUTHORIZED`、`FORBIDDEN`、`NOT_FOUND`、`CONFLICT` 或 `HTTP_ERROR`。 |
| `RequestValidationError` | `_validation_error_handler` | 422 + `VALIDATION_ERROR`，`details.errors` 保留 Pydantic 错误。 |

`HTTPException` 也已经被包装成标准信封。比如 `require_scope()` 抛出的 403 不再是裸 `{"detail": ...}`，而是：

```json
{
  "error": {
    "code": "FORBIDDEN",
    "message": "Missing required scope(s): config:write",
    "request_id": "req_xxx",
    "details": {}
  }
}
```

不走 JSON 错误信封的路径：

- `/metrics` 返回 Prometheus text。
- WebSocket 连接失败使用 close code/reason。
- 邮件 token 的 GET 确认页返回 HTML；部分 token 错误也返回 HTML error page。

## 3. Authentication and Scope Boundary

### API Key Middleware

`apps/api/middleware/auth.py` 的流程：

```text
if API_KEY_AUTH_ENABLED=false
  -> skip auth
if request path matches API_KEY_OPEN_PATHS
  -> skip auth
parse Authorization: Bearer <raw_key>
if raw_key matches API_KEY_INITIAL_SEED and no key exists
  -> request.state.api_key = bootstrap identity
else
  -> ApiKeyService.verify(raw_key)
  -> request.state.api_key = identity
after response
  -> best-effort touch last_used_at
```

Open path 匹配会先用 `os.path.normpath()` 规范化路径，再做边界感知匹配：路径必须等于 open path，或以 `<open_path>/` 开头。

Bootstrap seed 只用于初始化第一个正式 API key：

- 身份是 `apik_initial`。
- scope 只有 `api_key:admin`。
- 只要 `api_keys` 表中已经存在任意 key，包括 revoked/expired key，seed 就会被拒绝。

### Scope Dependency

`require_scope()` / `require_any_scope()` 在 `apps/api/dependencies.py`：

- 当 `API_KEY_AUTH_ENABLED=false` 时直接放行。
- 当 auth enabled 时，从 `request.state.api_key["scopes"]` 读取 scopes。
- 缺 identity 返回 401。
- scope 不匹配返回 403。

当前 route-level scope enforcement：

| Scope | 使用位置 |
|-------|----------|
| `api_key:admin` | `/api/api-keys` 创建、列表、撤销。 |
| `config:read` / `config:write` | `/api/config/current`、versions、overrides 读写。 |
| `config:write` | config publish、rollback、revoke、override create/revoke。 |
| `discovery:read` / `discovery:write` | discovery GET endpoints。 |
| `discovery:write` | discovery rerun。 |
| `runbook:review` / `runbook:llm_generate` | LLM runbook draft。 |
| `runbook:review` + `runbook:web_search` | Runbook Web search。 |
| `runbook:review` + `incident:llm_diff` | Incident/runbook diff。 |
| `llm:invoke` 或 `ai:external` | 外部 LLM incident diff 的附加权限。 |

普通 incident、approval、action、report endpoints 当前主要依赖全局 API key auth 和服务层状态校验，不都有 route-level scope。新增敏感 endpoint 时，需要明确是否只依赖全局 auth，还是必须加 scope。

## 4. Dependency Injection

`apps/api/dependencies.py` 提供控制面依赖：

| 依赖 | 用途 |
|------|------|
| `get_db()` | SQLAlchemy session generator。 |
| `get_app_settings()` | cached `Settings`。 |
| `get_task_enqueue()` | `enqueue_diagnosis_task`。 |
| `get_resume_task_enqueue()` | `enqueue_resume_task`。 |
| `get_notification_task_enqueue()` | `enqueue_email_notification_task`。 |
| `get_current_api_key()` | 读取 `request.state.api_key`；auth disabled 时通常是 `{}`。 |
| `require_scope()` | FastAPI scope dependency。 |

Service 层负责 commit/rollback。大多数业务 router 只实例化 service 并返回 schema。

当前有少量控制面 router 直接调用 helper/repository 并 commit：

- `apps/api/routers/config.py` 调用 `ConfigPublisher` 和 override repository。
- `apps/api/routers/discovery.py` 在 rerun endpoint 中创建 discovery run、获取 Redis lock、入队 task、写 audit。
- `apps/api/routers/incidents.py` 的 audit list 是只读 repository 查询。

这些是现有控制面实现的例外；新增普通业务能力仍优先保持 `router -> service -> repository`。

## 5. Alert Ingestion Path

`POST /api/alerts` 是控制面最重要的写路径：

```text
Router
  -> AlertCreateRequest provider payload normalization
  -> RateLimiter.is_allowed("alerts", api_key_id_or_ip)
  -> AlertService.create_alert()

AlertService
  -> FalsePositivePatternRepository.should_suppress()
  -> IncidentRepository.get_open_by_fingerprint()
  -> if existing open incident: return deduplicated response
  -> create Incident and AgentRun
  -> commit
  -> enqueue_diagnosis(incident_id, agent_run_id)
  -> set celery_task_id
  -> commit
  -> enqueue notification best-effort
```

关键边界：

- API 线程只创建 incident/run 并入队，不运行 LangGraph。
- fingerprint 去重只复用未终态 incident。
- NFA suppressed alert 会把新 incident severity 降到 `P4`，但仍保留记录。
- 第一次 commit 发生在 Celery 入队前，确保 worker 的独立 DB 连接能读到 incident/run。
- 若 enqueue 失败，service 会把 agent run 标记为 failed，把 incident 移到 failed，然后抛 `DependencyUnavailableError`。
- notification 入队失败不会阻断告警摄取。

## 6. Manual Diagnosis Path

`POST /api/incidents/{incident_id}/diagnose`：

```text
IncidentService.trigger_diagnosis()
  -> require incident exists
  -> if active run exists and force=false: 409 Conflict
  -> create new AgentRun
  -> commit
  -> enqueue diagnosis
  -> set celery_task_id
  -> commit
```

`force=true` 不删除旧 run，只创建新 run。这样保留历史轨迹和报告引用，不把正在运行或失败的 run 覆盖掉。

如果 enqueue callable 未配置，会抛 `DependencyUnavailableError`。如果 enqueue 调用抛异常，service 会标记新 run 为 failed 并提交。

## 7. Approval Decision Path

审批路径的核心目标是：先持久化人工决策，再让 worker 用同一个 checkpoint config 恢复。

### Single Decision

```text
POST /api/approvals/{approval_id}/approve|reject
  -> ApprovalService._require_approval()
       -> ApprovalRepository.get_for_update()
  -> validate waiting status
  -> load Action
  -> if approve L3: validate risk_ack/action_type/target
  -> update approval status
  -> update action status
  -> create audit log
  -> commit
  -> if no waiting approval remains for run: enqueue_resume(agent_run_id, decision)
```

L3 approve 必须满足：

```text
risk_ack == true
confirm_action_type == action.type
confirm_target == (action.target or "")
```

Reject 不需要 L3 二次确认。当前前端要求填写拒绝原因；后端 schema 允许 comment 为空。

### Batch Decision

`POST /api/approvals/batch` 先做整批 preflight：

- `approval_ids` 不能重复。
- 每个 approval 必须存在并处于 waiting。
- 每个 action 必须存在。
- 批量 approve 中最多只能包含一个 L3 approval；该 L3 必须提供匹配的二次确认字段。

只要 preflight 有错误，整批不更新，返回 `ValidationAppError`。这避免 L2 已批准、同批 L3 失败的部分成功状态。

当前前端在选中 L3 时禁用批量批准；后端仍保留 L3 batch preflight 作为最终边界。

### Email Token

Email token 行为：

- `generate_email_token()` 生成 24 小时 token 并提交。
- L2 可以通过 email token approve。
- L3 不能通过 email approve，必须回到 Web 控制台填写二次确认。
- approve/reject by token 成功后清空 token。
- token 过期时清空 token 并返回业务校验错误。

## 8. Action Execute Endpoint

`POST /api/actions/{action_id}/execute` 是手动执行端点，当前只使用 `FixtureExecutorBackend`：

```text
ActionService.execute()
  -> require action exists
  -> if L4: mark blocked, commit, raise ApprovalRequiredError
  -> if L2/L3: require approved approval
  -> if L3: re-check saved risk_ack/action_type/target
  -> reject already executing/succeeded
  -> mark executing
  -> FixtureExecutorBackend.execute()
  -> mark succeeded and save execution_result
  -> commit
```

真实 live Kubernetes executor 不从这个手动 API 直接触发。live executor 只在 worker 图执行路径中通过 `EXECUTOR_BACKEND=live` 显式启用，并继续受 guardrail、approval、snapshot 和 verify 控制。

## 9. API Key Lifecycle

API key 管理路由整体挂载 `Depends(require_scope("api_key:admin"))`。

创建路径：

```text
POST /api/api-keys
  -> ApiKeyCreateRequest validates scopes and roles
  -> ApiKeyService.create()
       -> secrets.token_hex(32)
       -> sha256(raw_key)
       -> repository.create(...)
       -> commit
       -> return raw_key once
```

验证路径：

```text
raw bearer key
  -> sha256(raw_key)
  -> ApiKeyRepository.get_by_hash()
  -> reject missing / revoked / expired
  -> return identity with key_id, created_by, scopes, roles
```

Raw key 不落库；列表接口只返回 metadata。`last_used_at` 在 middleware 响应后 best-effort 更新，失败只记录 warning，不影响用户请求。

## 10. Config and Discovery Control Plane

Config/discovery 属于 operator 控制面，当前比普通业务 API 更依赖 route-level scope。

### Config

`apps/api/routers/config.py` 使用 `ConfigPublisher` 和 override repository：

| 路径 | 关键行为 |
|------|----------|
| `GET /api/config/current` | 读取 latest published config；没有则返回 `status="none"`。 |
| `GET /api/config/versions` | 列出最近版本。 |
| `POST /api/config/publish` | 发布新 EffectiveConfigVersion，旧 published 版本被 supersede。 |
| `POST /api/config/rollback` | 回滚到最近 superseded 版本。 |
| `POST /api/config/revoke` | 撤销版本，使 worker 不再选它。 |
| `GET/POST/DELETE /api/config/overrides` | 管理 active override。 |

Override 写路径有两层保护：

- 禁止字段包括 secret/auth/executor/live/bearer token/password/private key/client cert 等。
- 如果 override 中有 URL，则用 `BackendUrlSafetyValidator` 校验。

Config router 当前捕获 `ConfigPublishError` / rollback / revoke 错误并抛 `HTTPException`。这些错误会被 `main.py` 的 HTTPException handler 包装成标准错误信封。

### Discovery

Discovery GET endpoints 从 discovery run summary、proposal 和 published config snapshot 重建展示数据，不直接影响 worker runtime config。

`POST /api/discovery/rerun`：

```text
create and flush DiscoveryRun(source="manual_rerun")
-> try RedisLock("discovery:runner", ttl=300)
-> enqueue_discovery_rerun_task()
-> create discovery audit log
-> commit
```

如果另一个 discovery run 持有锁，响应 `status="locked"`；成功入队后才写 audit 并提交事务。Discovery rerun 只产生 run/proposal；生产环境不会因为 rerun 自动发布 worker 配置。

## 11. Reports, Feedback, Comments and Audit

### Reports

`ReportService.regenerate()`：

- 要求 incident 存在。
- 使用 latest agent run。
- 读取 evidence/actions。
- 优先使用 `run.state["incident_report"]` 中已有字段。
- 否则根据 incident/evidence/actions 生成 fallback 内容。
- 使用 `reports.next_version(incident_id)` 创建新版本。
- commit 后 best-effort 发送 `incident_report` 通知。

重新生成报告不会覆盖旧版本。

### Feedback and Comments

Feedback/comment service 会写业务表并写 audit log：

- NFA 标记。
- 根因修正。
- action feedback。
- comment 创建。
- evidence annotation 创建。

Audit log 由 `AuditLogRepository.create()` 写入 `adt_` 前缀 ID。repository 不提供 update/delete 方法。

当前不是所有 audit 写入都会携带 HTTP request ID；需要强 request-id 关联的新写路径，应显式把 `request.state.request_id` 传入 service/repository。

## 12. Rate Limit

`RateLimiter` 是 Redis sorted-set sliding window：

```text
key = ratelimit:{scope}:{identifier}
zremrangebyscore(expired)
zcard()
zadd(now)
expire()
```

当前 alert ingestion 使用：

- scope: `alerts`
- identifier: API key ID；无 key 时用 client IP
- 默认：10 requests / 60 seconds

Redis 不可用时 fail open，记录 warning 并放行请求。这个设计避免监控/告警系统因为 Redis 短暂故障而完全无法上报告警。

## 13. WebSocket Control Plane

WebSocket ticket 和实时事件的完整前端链路见 [前端控制台与实时更新技术深挖](frontend-realtime-console-deep-dive.md)。API 控制面这里的核心边界是：

- HTTP ticket endpoint 使用普通 bearer API key。
- WebSocket handshake 只接收短期 incident-scoped ticket。
- `API_KEY_AUTH_ENABLED=true` 时缺 ticket 或无效 ticket 会关闭连接。
- Redis Pub/Sub 发布失败只记录 warning，不影响 worker 诊断。

WebSocket 是实时展示增强，不是 Agent run 状态推进的 source of truth。

## 14. Transaction Rules

| 路径 | 提交点 | 原因 |
|------|--------|------|
| Alert ingestion | 创建 incident/run 后先 commit，再 enqueue；task id 设置后再 commit。 | Worker 独立连接必须读到 run；enqueue 失败要标记 failed。 |
| Manual diagnose | 创建 run 后 commit，再 enqueue；task id 设置后 commit。 | 保留 run 历史并让 worker 可见。 |
| Approval decision | 更新 approval/action/audit 后 commit，再 maybe resume。 | Worker resume 前必须读到人工决策。 |
| Batch approval | preflight 全部通过后更新整批，commit 后按 run maybe resume。 | 避免部分成功。 |
| API key create/revoke | repository 写入后 commit。 | raw key 只返回一次；revoke 立即生效。 |
| Config publish/rollback/revoke | publisher 成功后 commit；异常 rollback。 | 保证 published config 状态一致。 |
| Report regenerate | 创建新 report version 后 commit，再 best-effort 通知。 | 版本不可覆盖，通知不阻断报告生成。 |

新增 service 时不要把 Celery enqueue 放在尚未提交的 DB 事务前，除非 worker 不需要读取这批数据。

## 15. Safety Boundaries

API 控制面必须保持这些边界：

- `POST /api/alerts` 和 manual diagnose 只入队，不内联运行 LangGraph。
- route-level scope 不替代服务层状态校验；approval/action 状态仍必须在 service 内重新检查。
- L3 二次确认必须由后端校验，不能只依赖前端。
- L4 action 不执行；手动 execute 也必须拒绝。
- 手动 action execute 使用 fixture executor，不触发 live Kubernetes backend。
- Config override 不能写 secret/auth/executor/live 字段。
- Discovery rerun 不自动 publish 生产配置。
- Raw API key、bearer token、password、private key 不得进入 error details、audit details、logs、prompt 或 report。

## 16. Add or Change an API

新增 HTTP endpoint 的推荐步骤：

1. 在 `apps/api/schemas/` 定义 request/response schema。
2. 在 `packages/db/repositories/` 增加必要查询或写入方法。
3. 在 `apps/api/services/` 编排业务规则、事务、审计和 task enqueue。
4. 在 `apps/api/routers/` 添加薄 router，绑定 response model 和 dependency。
5. 如果 endpoint 敏感，添加 `require_scope()`，并更新 API key scope allowlist。
6. 如果写入跨越外部系统或 worker，明确 commit 和 enqueue 顺序。
7. 更新 API 参考、后端架构或本深挖文档。
8. 增加对应 unit/integration/contract/API client 测试。

不要在 router 中直接拼 SQL、直接创建 Celery task payload 后跳过 service 状态校验，也不要把 `HTTPException` 当成隐藏业务分支来规避统一错误处理。

## 17. Test Coverage Map

相关测试入口：

| 主题 | 测试文件 |
|------|----------|
| Alert ingestion、request id、dedup、manual diagnose | `tests/integration/test_alert_api.py` |
| Approval L2/L3、resume、batch、email token | `tests/integration/test_approval_api.py`、`tests/integration/test_phase6_collaboration.py` |
| API key admin 和 auth | `tests/integration/test_api_key_admin_api.py`、`tests/unit/test_api_key_service.py` |
| Config scopes and behavior | `tests/integration/test_config_api.py`、`tests/integration/test_config_api_auth.py` |
| Override scopes and URL safety | `tests/integration/test_override_api.py`、`tests/integration/test_override_api_auth.py` |
| Discovery read/write auth and rerun | `tests/integration/test_discovery_api.py`、`tests/integration/test_discovery_api_auth.py`、`tests/integration/test_discovery_rerun_api.py` |
| Rate limiter | `tests/unit/test_rate_limit.py` |
| Report API | `tests/integration/test_report_api.py` |
| Runbook API contract | `tests/contract/test_runbook_api_contract.py` |

按项目策略，Codex 不直接运行 pytest。需要本地验证时由用户执行：

```bash
pytest tests/unit tests/integration --cov=apps --cov=packages --cov-report=term-missing --cov-report=xml --cov-fail-under=80
```

## 18. Debug Checklist

| 现象 | 先看 | 常见原因 |
|------|------|----------|
| 页面一直 401 | `API_KEY_AUTH_ENABLED`、open paths、Authorization header、API key revoked/expired | 未传 bearer、key 已撤销、bootstrap seed 已失效。 |
| 403 forbidden | `request.state.api_key.scopes`、router `require_scope()` | 缺 `config:write`、`discovery:write`、M9 runbook scope。 |
| 429 on alerts | Redis rate-limit key、API key ID/client IP、settings | 同一 key/IP 超过 alert ingestion 限制。 |
| Alert 入队失败 | API response request id、agent_run 状态、worker/Celery broker | Celery enqueue 异常；service 应标记 run/incident failed。 |
| Manual diagnose 409 | active run | `force=false` 且 incident 已有 queued/running/waiting run。 |
| 审批后 run 未恢复 | approvals status、`has_waiting_for_run()`、resume task enqueue | 同 run 仍有 waiting approval，或 worker/Celery 不在线。 |
| L3 approve 失败 | action target/type、payload confirmation fields | `risk_ack` 缺失或确认字段不匹配。 |
| Config publish 400 | HTTP error details、publisher validation | config snapshot 形状错误或 publish 规则不满足。 |
| Discovery rerun locked | Redis lock `discovery:runner` | 另一个 discovery run 正在执行。 |
| Report 404 | incident report table | incident 尚无 report version。 |
