# 错误响应与 Request ID

## Request ID middleware

每个请求都会经过 request id middleware：

- 优先读取请求头 `X-Request-Id`。
- 缺失时生成 `req_` 前缀 ID。
- 写入 `request.state.request_id`。
- 在响应 header 中返回 `X-Request-Id`。

## 标准错误 envelope

所有 `AppError` 渲染为：

```json
{
  "error": {
    "code": "NOT_FOUND",
    "message": "incident not found",
    "request_id": "req_abc",
    "details": {
      "id": "inc_123"
    }
  }
}
```

校验错误渲染为：

```json
{
  "error": {
    "code": "VALIDATION_ERROR",
    "message": "request validation failed",
    "request_id": "req_abc",
    "details": {
      "errors": []
    }
  }
}
```

## 错误类型

| 类 | code | HTTP | 用途 |
| --- | --- | --- | --- |
| `ValidationAppError` | `VALIDATION_ERROR` | 400 | 业务校验失败 |
| `NotFoundError` | `NOT_FOUND` | 404 | public ID 不存在 |
| `ConflictError` | `CONFLICT` | 409 | 重复操作、状态冲突 |
| `DependencyUnavailableError` | `DEPENDENCY_UNAVAILABLE` | 503 | 外部依赖或 checkpointer 不可用 |
| `ApprovalRequiredError` | `APPROVAL_REQUIRED` | 403 | 缺少审批或动作不可直接执行 |

API key middleware 的鉴权失败返回：

```json
{
  "error": {
    "code": "UNAUTHORIZED",
    "message": "unauthorized",
    "request_id": "req_abc",
    "details": {}
  }
}
```

## 实现约束

- 不应在 router 中直接返回 ad hoc 错误结构。
- 业务异常应抛出 `AppError` 子类。
- Pydantic 校验错误由 FastAPI exception handler 统一转换。
- 错误 body 和响应 header 的 request id 必须一致。
- 重要依赖失败不能被静默吞掉；工具层可返回 degraded result，关键 gate 如 checkpointer 必须 fail closed。
