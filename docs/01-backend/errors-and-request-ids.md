# 错误响应与请求 ID

**最后更新：** 2026-06-14

## Request ID Middleware

`apps/api/main.py` 中的 `_request_id_middleware`：

1. 读取传入 `X-Request-Id`。
2. 缺失时通过 `new_id("req_")` 生成新 ID。
3. 写入 `request.state.request_id`。
4. 调用下游 handler。
5. 在响应头设置同一个 `X-Request-Id`。

所有新写 API 都应支持 request ID。错误响应体中的 `request_id` 必须与响应头一致。

## 标准错误信封

`AppError` 由 `_app_error_handler` 渲染为：

```json
{
  "error": {
    "code": "NOT_FOUND",
    "message": "incident not found",
    "request_id": "req_abc123",
    "details": {
      "id": "inc_xxx"
    }
  }
}
```

Pydantic `RequestValidationError` 由 `_validation_error_handler` 渲染为 HTTP 422：

```json
{
  "error": {
    "code": "VALIDATION_ERROR",
    "message": "request validation failed",
    "request_id": "req_abc123",
    "details": {
      "errors": [
        {"loc": ["body", "severity"], "msg": "field required", "type": "missing"}
      ]
    }
  }
}
```

API key middleware 的认证失败返回 HTTP 401，并使用同一信封：

```json
{
  "error": {
    "code": "UNAUTHORIZED",
    "message": "unauthorized",
    "request_id": "req_abc123",
    "details": {}
  }
}
```

## 当前例外：HTTPException

部分 router 或 dependency 当前直接抛出 FastAPI `HTTPException`，例如：

- `require_scope()` 缺少认证身份或 scope 时返回 401/403。
- `config` router 捕获 publish/rollback/revoke/override 错误时返回 400/404。

这些响应目前使用 FastAPI 默认结构，例如：

```json
{"detail": "Missing required scope(s): config:write"}
```

新增业务错误应优先使用 `AppError` 子类，除非明确需要保持 FastAPI 默认行为。

## AppError 类型

`packages/common/errors.py`：

| 类 | Code | HTTP | 用途 |
|----|------|------|------|
| `AppError` | 自定义 | 400 默认 | 基类，直接渲染标准信封 |
| `ValidationAppError` | `VALIDATION_ERROR` | 400 | 业务校验失败，不是 Pydantic schema 422 |
| `NotFoundError` | `NOT_FOUND` | 404 | 公共 ID 未找到；`details.id` 自动设置 |
| `ConflictError` | `CONFLICT` | 409 | 状态冲突、重复决策、active run 冲突 |
| `DependencyUnavailableError` | `DEPENDENCY_UNAVAILABLE` | 503 | Celery、checkpointer、外部依赖不可用且必须故障关闭 |
| `ApprovalRequiredError` | `APPROVAL_REQUIRED` | 403 | 缺少审批、L4 不可执行或 L3 二次确认缺失 |
| `TooManyRequestsError` | `TOO_MANY_REQUESTS` | 429 | 速率限制 |

## 常见错误场景

| 场景 | HTTP | Code / 结构 | 说明 |
|------|------|-------------|------|
| Pydantic 请求体验证失败 | 422 | `VALIDATION_ERROR` 信封 | `details.errors` 来自 Pydantic |
| 未认证 API key | 401 | `UNAUTHORIZED` 信封 | 由 auth middleware 返回 |
| 缺少 scope | 403 | FastAPI `detail` | 由 `require_scope()` 抛 `HTTPException` |
| 未找到 public ID | 404 | `NOT_FOUND` 信封 | `NotFoundError` |
| active run 冲突 | 409 | `CONFLICT` 信封 | 手动 diagnose `force=false` 等 |
| 告警摄取限流 | 429 | `TOO_MANY_REQUESTS` 信封 | Redis sliding-window rate limit |
| Celery 入队失败 | 503 | `DEPENDENCY_UNAVAILABLE` 信封 | alert service 会把 run/incident 标记失败 |
| Checkpointer 初始化失败 | 503 | `DEPENDENCY_UNAVAILABLE` 信封 | Worker 故障关闭，避免绕过审批 gate |
| L4 action execute | 403 | `APPROVAL_REQUIRED` 信封 | L4 永不执行 |

## Rate Limit

`apps/api/rate_limit.py` 使用 Redis sorted set sliding window：

- Key：`ratelimit:{scope}:{identifier}`。
- Identifier：API key ID；未认证时使用 client IP。
- 配置：`RATE_LIMIT_MAX_REQUESTS`、`RATE_LIMIT_WINDOW_SECONDS`。
- 当前 alert ingestion 使用该 limiter，默认 10 requests / 60 seconds。
- Redis 不可用时故障开放，避免误阻断合法告警。

## 审计日志

`AuditLog` 是 append-only 业务日志。关键字段：

| 字段 | 说明 |
|------|------|
| `audit_id` | `adt_` 前缀公共 ID；worker poll helper 仍可能产生 `aud_` 兼容记录 |
| `incident_id` | 可空 incident 关联 |
| `actor` | API key、operator、worker、system 等 |
| `action` | 操作名，例如 `approve`、`reject`、`discovery.rerun_complete` |
| `resource_type` / `resource_id` | 被操作资源 |
| `source` | `api`、`worker`、`beat`、`system` 等来源 |
| `request_id` | 与 HTTP request ID 关联；worker/system 可为空或使用内部 ID |
| `details` | JSON 详情，不能包含原始密钥 |

代码层不提供 audit log 更新/删除路径。生产环境如需强不可变，应在数据库层增加触发器或权限约束。

## 实现规则

- 新业务错误优先抛 `AppError` 子类，不在 router 里拼临时错误 JSON。
- 不手动捕获 Pydantic validation error；交给 FastAPI exception handler。
- Response header 和 error body 中的 request ID 必须一致。
- 安全关键依赖故障应故障关闭，例如 checkpointer 不可用时不能继续运行审批路径。
- 工具层数据源故障可返回 degraded result；guardrail、approval、auth 不应故障开放。
- 错误 details 中不得包含 raw secret、API key、bearer token、password、private key。
