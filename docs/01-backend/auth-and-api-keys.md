# API Key 鉴权

**最后更新：** 2026-06-14

## 默认行为

`packages/common/settings.py` 中 `API_KEY_AUTH_ENABLED` 默认是 `true`。`docker-compose.yml` 为本地 demo 显式设置为 false，方便无 key 试用控制台和 API。

| 运行方式 | 默认认证行为 |
|----------|--------------|
| 直接 `uvicorn apps.api.main:app` | API key auth enabled |
| Docker Compose demo | API key auth disabled |
| 测试环境 | 通常通过 settings override 或 fixture 关闭 auth |

开放路径来自 `API_KEY_OPEN_PATHS`，默认值：

```text
/healthz,/readyz,/metrics,/docs,/openapi.json,/api/approvals/by-token
```

路径匹配是规范化后的边界感知前缀匹配。因此 `/docs/oauth2-redirect` 会被 `/docs` 覆盖。`/redoc` 当前不在默认开放路径中。

## HTTP 认证

受保护 HTTP endpoint 需要：

```text
Authorization: Bearer <api_key>
```

认证中间件流程在 `apps/api/middleware/auth.py`：

1. 如果 `api_key_auth_enabled=false`，跳过所有认证检查。
2. 如果请求 path 命中开放路径，跳过认证。
3. 读取 `Authorization: Bearer ...`。
4. 若 `API_KEY_INITIAL_SEED` 存在，使用恒时比较检查 bootstrap key。
5. 否则通过 `ApiKeyService.verify(raw_key)` 查 hash、撤销状态和过期时间。
6. 成功后写入 `request.state.api_key`。
7. 响应后 best-effort 更新 `last_used_at`。

认证失败返回标准错误信封，HTTP 401，`code=UNAUTHORIZED`。

## WebSocket 认证

WebSocket endpoint：

```text
/api/ws/incidents/{incident_id}?token=<api_key>
```

认证失败关闭连接，code 为 `4001`。Auth disabled 时跳过 token 校验。

## ApiKey 模型

| 字段 | 说明 |
|------|------|
| `key_id` | 公共 ID，前缀 `apik_` |
| `description` | 人类可读描述 |
| `key_hash` | raw key 的 SHA-256 hash；raw key 不落库 |
| `created_by` | 创建者，当前默认 `admin` |
| `roles` | JSON list，当前主要用于元数据 |
| `scopes` | JSON list，供 `require_scope()` 使用 |
| `is_bootstrap` | bootstrap 标记 |
| `expires_at` | 可选过期时间 |
| `last_used_at` | best-effort 最近使用时间 |
| `revoked` | 撤销标记 |

`ApiKeyCreateRequest` 当前只接受：

```json
{
  "description": "production-operator-key",
  "expires_in_days": 90
}
```

创建响应中的 `raw_key` 只返回一次。之后列表接口只返回 metadata，不返回 raw key。

## Scope 系统

`apps/api/dependencies.py` 提供 `require_scope()` / `require_any_scope()`。当 auth disabled 时，scope dependency 直接放行；当 auth enabled 时，它从 `request.state.api_key["scopes"]` 中检查是否具有任意一个要求的 scope。

当前 route-level scope enforcement：

| Scope | 使用位置 | 行为 |
|-------|----------|------|
| `config:read` | `GET /api/config/current`、`GET /api/config/versions`、`GET /api/config/overrides` | 读取配置 |
| `config:write` | config publish/rollback/revoke/override write | 写配置；也可读配置 |
| `discovery:read` | discovery GET endpoints | 读取发现状态 |
| `discovery:write` | discovery rerun；也可读 discovery | 触发手动发现 |
| `runbook:review` | M9 runbook LLM/Web 能力；incident diff 还需 `incident:llm_diff` | 审核/高级 runbook 能力 |
| `runbook:llm_generate` | `POST /api/runbooks/llm-generate` | LLM runbook draft |
| `runbook:web_search` | `POST /api/runbooks/web-search`，需同时具备 `runbook:review` | Web search enrichment |
| `incident:llm_diff` | `POST /api/runbooks/incident-diff`，需同时具备 `runbook:review` | Incident/runbook diff |

外部云 LLM 的 incident diff 还需要 `llm:invoke` 或 `ai:external`。定义但当前未直接在 router 中强制的 scope 常量包括 `runbook:read`、`embedding:external`。API key 管理 endpoints 当前没有 route-level `api_key:admin` dependency；它们只受全局 API key auth 保护。

## Bootstrap Key 注意事项

`API_KEY_INITIAL_SEED` 用于初始认证。当前 middleware 将其身份设置为：

```json
{
  "key_id": "apik_initial",
  "description": "initial-seed",
  "created_by": "system"
}
```

该身份当前不包含 scopes，因此在 auth enabled 且 route 要求 scope 时，bootstrap key 不能通过 scope dependency。需要 scope 的配置/发现/M9 endpoint 应使用数据库中的 API key，或在本地/demo 中关闭 auth。

## 后端认证配置

`packages/common/backend_auth.py` 支持后端连接认证配置：`none`、`bearer`、`basic`、`mtls`。原则：

- 原始密钥使用 `env:VAR_NAME` 引用。
- 运行时解析密钥；不要把 raw secret 写进 DB、审计日志、AgentDeps、LLM prompt 或 report。
- 后端 URL 还必须通过 `BackendUrlSafetyValidator`，生产环境拒绝 localhost、link-local、metadata endpoint 等危险目标。

## 开发注意事项

- 新增敏感 endpoint 时，先决定是否只需要全局 auth，还是还需要 `require_scope()`。
- 若引入新 scope，要同时更新 schema/service 测试、API 文档和配置/运维文档。
- 不要在响应、日志、审计或错误 details 中返回 raw key。
- `last_used_at` 更新是 best-effort，失败只写 warning，不影响请求结果。
