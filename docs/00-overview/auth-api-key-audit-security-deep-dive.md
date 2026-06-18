# 认证、API Key、审计与安全边界技术深挖

**最后更新：** 2026-06-17

本文补充 [认证与 API 密钥](../01-backend/auth-and-api-keys.md)、[API 控制面与服务层技术深挖](api-control-plane-service-deep-dive.md)、[数据模型、迁移与持久化技术深挖](data-model-migrations-persistence-deep-dive.md)、[配置参考](../11-reference/configuration.md) 和 [生产发布、运维与回滚技术深挖](production-operations-rollback-deep-dive.md)。它从代码路径解释当前 HTTP API 认证、scope、bootstrap key、WebSocket ticket、rate limit、audit log、secret redaction 和生产启用检查如何组合。

## 一句话模型

当前安全控制面由五层组成：

```text
request id middleware
  -> API key auth middleware
  -> route-level scope dependency
  -> service/repository business guard
  -> audit/redaction/persistence boundary
```

关键边界：

- `API_KEY_AUTH_ENABLED=true` 时，非开放 HTTP path 必须带 `Authorization: Bearer <api_key>`。
- `API_KEY_AUTH_ENABLED=false` 时，HTTP auth 和 scope dependency 都跳过；这是本地 demo/测试便利，不是生产口径。
- API key 的 raw value 只在创建响应中返回一次；数据库只保存 SHA-256 hash。
- Bootstrap seed 只在 `api_keys` 表为空时可用，且只拥有 `api_key:admin`。
- WebSocket 不把长期 API key 放在 URL 中；浏览器先用 HTTP bearer key 换短期 ticket。
- Audit log 是代码层 append-only；生产若需要强不可变，应增加 DB trigger 或权限策略。
- 原始 secret 不应进入 DB、audit、state、prompt、report 或前端响应。

## 代码入口

| 主题 | 代码入口 | 说明 |
|------|----------|------|
| App middleware 顺序 | `apps/api/main.py` | CORS、GZip、request id、API key middleware、异常处理器、router 注册 |
| HTTP API key middleware | `apps/api/middleware/auth.py` | 开放路径、Bearer key、bootstrap seed、hash verify、`last_used_at` |
| Scope dependency | `apps/api/dependencies.py` | `require_scope()` / `require_any_scope()`；auth disabled 时跳过 |
| API key router/service | `apps/api/routers/api_keys.py`、`apps/api/services/api_key_service.py` | create/list/revoke，raw key one-time return |
| API key repository/model | `packages/db/repositories/api_keys.py`、`packages/db/models.py` | hash、scopes、roles、expiry、revoked、last_used_at |
| API key schema allowlist | `apps/api/schemas/api_keys.py` | scope allowlist、role 格式、expiry 上限 |
| WebSocket ticket | `apps/api/ws/router.py`、`apps/api/services/ws_ticket_service.py` | incident-scoped HMAC ticket，TTL 60-300 秒 |
| Rate limit | `apps/api/rate_limit.py`、`apps/api/routers/alerts.py` | alert ingestion sliding window，Redis 失败 fail open |
| Audit repository | `packages/db/repositories/audit_logs.py` | create/query-only，业务层 append-only |
| Redaction | `packages/common/redaction.py`、`packages/common/backend_auth.py` | prompt/audit/external context 脱敏与 runtime-only secret |

## HTTP 认证路径

`apps/api/main.py` 注册顺序里，request ID middleware 和 API key middleware 都是 HTTP middleware：

```text
incoming HTTP request
  -> request_id middleware: 读取或生成 X-Request-Id
  -> api_key middleware: 鉴权并写 request.state.api_key
  -> router/dependency/service
  -> exception handler 统一错误信封
```

认证中间件流程：

1. 读取 `Settings.api_key_auth_enabled`。
2. 若为 `false`，直接放行。
3. 若 path 命中 `API_KEY_OPEN_PATHS`，直接放行。
4. 要求 `Authorization: Bearer <raw_key>`。
5. 若 raw key 等于 `API_KEY_INITIAL_SEED`，只有 key store 为空时才接受。
6. 否则用 `ApiKeyService.verify()` 将 raw key hash 后查库。
7. 拒绝 revoked、expired 或不存在的 key。
8. 成功后将 identity 写入 `request.state.api_key`。
9. 响应后 best-effort 更新 `last_used_at`；失败只写 warning，不影响请求。

认证失败返回标准错误信封：

```json
{
  "error": {
    "code": "UNAUTHORIZED",
    "message": "unauthorized",
    "request_id": "req_...",
    "details": {}
  }
}
```

## 开放路径

默认 `API_KEY_OPEN_PATHS`：

```text
/healthz,/readyz,/metrics,/docs,/openapi.json,/api/approvals/by-token
```

实现细节：

- 路径先经过 `os.path.normpath()` 规范化。
- 匹配是边界感知前缀匹配：path 等于 open path，或以 `<open_path>/` 开头。
- 因此 `/docs/oauth2-redirect` 会被 `/docs` 覆盖。
- `/redoc` 不在默认开放路径中。
- `/api/approvals/by-token` 是 email token 审批入口，不等于普通 approval API 无认证。

新增开放路径要谨慎。开放 path 绕过所有 API key 身份注入，也会让后续 scope dependency 没有身份可检查。

## Scope 系统

`require_scope(*scopes)` 的语义是“具备任意一个 scope 即可”。例如：

```python
require_scope("config:read", "config:write")
```

表示有 `config:read` 或 `config:write` 都能通过。

当 `API_KEY_AUTH_ENABLED=false` 时，scope dependency 直接放行。生产环境不能依赖 route-level scope 来弥补关闭全局 auth 的风险。

当前 scope allowlist：

| Scope | 用途 |
|-------|------|
| `api_key:admin` | 创建、列表、撤销 API key |
| `config:read` | 读取当前配置、版本、override |
| `config:write` | 发布、回滚、撤销配置，创建/撤销 override；同时可读配置 |
| `discovery:read` | 读取 discovery status/services/metrics/topology/capabilities |
| `discovery:write` | 手动 discovery rerun；同时可读 discovery |
| `runbook:read` | runbook read scope，当前更多是预留/通用权限 |
| `runbook:review` | runbook review/M9 高级 runbook 能力基础权限 |
| `runbook:web_search` | M9 runbook web search |
| `runbook:llm_generate` | M9 LLM runbook draft |
| `incident:llm_diff` | M9 incident/runbook diff |
| `llm:invoke` | 外部 LLM 调用许可 |
| `ai:external` | 更宽的外部 AI provider 许可 |
| `embedding:external` | 外部 embedding provider 权限 |

`api_key:admin` 不隐含 `config:write`、`discovery:write` 或 M9 scopes。集成测试覆盖了这一点。

## 需要多个 scope 的路径

大多数 route 使用 `require_scope()` 的任一 scope 语义。当前有两个 M9 route 使用自定义校验，要求同时具备多个 scope：

| Endpoint | 要求 |
|----------|------|
| `POST /api/runbooks/web-search` | 必须同时具备 `runbook:review` 和 `runbook:web_search` |
| `POST /api/runbooks/incident-diff` | 必须同时具备 `runbook:review` 和 `incident:llm_diff` |

`incident-diff` 还有额外外部 provider 规则：

- 如果 `LLM_PROVIDER` 是 `openai`、`deepseek` 或 `anthropic`，还必须具备 `llm:invoke` 或 `ai:external`。
- Fake/local provider 路径不需要外部 LLM scope。

这类“必须同时具备多个 scope”的 endpoint 不应误用 `require_scope("a", "b")`，否则会变成任一 scope 即可。

## Bootstrap Seed

`API_KEY_INITIAL_SEED` 是初始引导凭据，不是长期管理员密码。

bootstrap identity 固定为：

```json
{
  "key_id": "apik_initial",
  "description": "initial-seed",
  "created_by": "system",
  "scopes": ["api_key:admin"],
  "roles": ["bootstrap"],
  "is_bootstrap": true
}
```

限制：

- 只有 `api_keys` 表中没有任何 key 时才接受。
- revoked 或 expired key 也算“已有 key”，因此 seed 不会成为恢复后门。
- 它只拥有 `api_key:admin`，不能直接发布配置、触发 discovery 或使用 M9 外部调用。
- 用它创建正式 operator key 后，应移除或轮换部署环境中的 seed。

推荐 bootstrap 流程：

1. 临时设置 `API_KEY_INITIAL_SEED`。
2. 调用 `POST /api/api-keys` 创建正式 key，按职责授予最小 scopes。
3. 移除或轮换 `API_KEY_INITIAL_SEED`。
4. 用正式 key 验证 API key admin、config/discovery/M9 所需权限。

## API Key 持久化

API key 创建路径：

```text
POST /api/api-keys
  -> require_scope("api_key:admin")
  -> ApiKeyCreateRequest 校验 scope allowlist、role 格式、expiry <= 365
  -> secrets.token_hex(32) 生成 raw key
  -> SHA-256 hash
  -> ApiKeyRepository.create()
  -> commit
  -> 响应返回 raw_key 一次
```

列表接口只返回 metadata：

- `key_id`
- `description`
- `created_by`
- `expires_at`
- `last_used_at`
- `revoked`
- `scopes`
- `roles`
- `created_at`

不会返回 `raw_key` 或 `key_hash`。撤销是设置 `revoked=True`，不是删除记录。

## WebSocket Ticket

浏览器 WebSocket 握手不能可靠携带 `Authorization` header，因此当前使用短期 ticket：

```text
POST /api/ws/incidents/{incident_id}/ticket
  Authorization: Bearer <api_key>
  -> WebSocketTicketService.issue()
  -> ticket + expires_at

WS /api/ws/incidents/{incident_id}?ticket=<ticket>
  -> verify HMAC、incident_id、exp
  -> accept
  -> subscribe Redis Pub/Sub incident:{incident_id}
```

ticket payload 包含：

- `incident_id`
- `key_id`
- `exp`
- `nonce`

生产约束：

- `WEBSOCKET_TICKET_SECRET` 在 `APP_ENV=production` 且 `API_KEY_AUTH_ENABLED=true` 时必须配置。
- 未配置时，ticket issue/verify 会失败，避免使用进程内临时 secret 造成多副本不可验证或安全假象。
- local/test 可使用进程内 `_LOCAL_TICKET_SECRET`。
- ticket TTL 由 `WEBSOCKET_TICKET_TTL_SECONDS` 控制，范围 1-300 秒，默认 60 秒。

## Rate Limit

当前 rate limit 只用于 `POST /api/alerts`：

```text
identifier = api_key.key_id 或 client IP
scope = "alerts"
Redis key = ratelimit:alerts:<identifier>
```

实现是 Redis sorted-set sliding window：

- `RATE_LIMIT_MAX_REQUESTS` 默认 10。
- `RATE_LIMIT_WINDOW_SECONDS` 默认 60。
- Redis 不可用时 fail open，允许请求继续，并写 warning。
- `memory://` Redis URL 使用进程内窗口，适合测试。

超过限制时抛出 `TooManyRequestsError`，由统一错误处理器返回 429 标准错误信封。

注意：rate limit 不是安全认证替代品。auth disabled 时，identifier 会退回 client IP。

## Audit Log

审计表 `audit_logs` 记录 who did what：

| 字段 | 用途 |
|------|------|
| `audit_id` | public ID，前缀 `adt_` |
| `incident_id` | 可为空；incident 相关审计用于事件详情页 |
| `actor` | 操作者、API key ID、system 或 worker |
| `action` | 操作名，例如 `approve`、`reject`、`config.publish` |
| `resource_type` / `resource_id` | 被操作对象 |
| `source` | `api`、`worker` 等来源 |
| `request_id` | HTTP request id 或任务上下文 |
| `details` | 脱敏后的上下文 |

当前主要写入路径：

- approval approve/reject/batch/email token 决策。
- comment 和 evidence annotation。
- feedback/NFA/root cause/action correction。
- config publish/rollback/revoke。
- discovery rerun、auto apply/reject、poll。
- M9 incident diff 创建 amendment draft。

`AuditLogRepository` 不提供 update/delete。生产需要更强不可变性时，使用数据库 trigger、权限或审计库策略补强。

## Secret Redaction

secret 边界分两类：

| 类型 | 代码 | 行为 |
|------|------|------|
| 后端连接 secret | `packages/common/backend_auth.py` | runtime-only；只允许 safe dict 进入日志/audit/state |
| 文本脱敏 | `packages/common/redaction.py` | 对 prompt、web search query、external embedding input、audit-safe text 做确定性 redaction |

当前 redaction 规则覆盖：

- Bearer token 和 Basic auth。
- API key header。
- token/secret/client_secret/password。
- private key block。
- URL 内嵌凭据。
- localhost、link-local、metadata endpoint、cluster internal URL。
- private IP。
- 常见 raw token 格式。
- namespace 和 service/app keyed references。

重要约束：

- `EffectiveConfigVersion.config_snapshot` 不应保存 raw secret；用 `env:VAR_NAME` 引用。
- `AuditLog.details` 不应保存 raw Authorization header、provider token、password、private key。
- LLM prompt、web search、external embedding 在出站前应脱敏。
- 脱敏元数据可以记录 redaction count/types，但不记录原值。

## 生产启用检查

生产发布前至少确认：

| 检查 | 期望 |
|------|------|
| `API_KEY_AUTH_ENABLED` | `true` |
| `API_KEY_OPEN_PATHS` | 只包含 health/metrics/docs 等明确开放路径和 email token 审批入口 |
| `API_KEY_INITIAL_SEED` | bootstrap 后移除或轮换 |
| 正式 API key | 按职责授予最小 scope，不使用万能 key |
| `WEBSOCKET_TICKET_SECRET` | 已配置稳定 secret |
| `RATE_LIMIT_MAX_REQUESTS` / `RATE_LIMIT_WINDOW_SECONDS` | 与告警量匹配 |
| Config override | 禁止 secret/auth/executor/live 字段 |
| Backend URL safety | 生产拒绝 localhost、metadata endpoint、危险 link-local |
| Audit | 关键写路径有审计记录，details 已脱敏 |
| External calls | LLM/Web/embedding 均有 feature flag、scope、timeout、redaction 和 degraded path |

## 新增敏感 API checklist

1. 判断 endpoint 是否应在开放路径中；默认不开放。
2. 若只需登录身份，依赖全局 API key auth 即可；若涉及高权限操作，必须加 `require_scope()` 或自定义多 scope 校验。
3. 如果必须同时具备多个 scope，不要用 `require_scope("a", "b")`，应写自定义 dependency。
4. 确认 auth disabled 时本地/CI 行为是否可接受。
5. 确认 request id 会进入错误信封和审计。
6. 不在 response、error details、audit details、日志中返回 raw key、Authorization header 或 provider secret。
7. 写入 audit 时只记录 public ID、scope 名、redacted summary 和 request id。
8. 涉及 WebSocket 时用短期 ticket，不把长期 key 放 URL。
9. 涉及外部调用时加 feature flag、timeout、redaction、audit/metric 和 degraded path。
10. 更新 API 文档、配置参考、测试策略和本深挖文档。

## 验证入口

Codex 按项目约束不直接运行 `pytest`、前端测试、Playwright 或完整测试套件。涉及认证或审计变更时，建议由开发者本地运行：

```bash
pytest tests/integration/test_api_key_admin_api.py -v
pytest tests/integration/test_config_api_auth.py -v
pytest tests/integration/test_discovery_api_auth.py -v
pytest tests/integration/test_amendment_draft_review.py -v
pytest tests/unit/test_backend_auth_redaction.py -v
```

按变更增加针对性用例：

- open path：路径规范化、边界前缀、`/redoc` 是否开放。
- bootstrap：空 store 可用、有任意 key 后拒绝。
- scope：无 key 401、缺 scope 403、任一 scope 与多 scope 语义区分。
- WebSocket ticket：缺失、过期、incident mismatch、production secret missing。
- rate limit：API key ID/IP 分桶、Redis fail open。
- audit：关键写路径创建审计，details 不含 raw secret。
- redaction：新增 secret 格式不会进入 prompt/audit/external request。
