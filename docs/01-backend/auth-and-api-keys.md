# API Key 鉴权

## 默认行为

当前配置：

- 直接运行 uvicorn：`API_KEY_AUTH_ENABLED=true`（settings.py 默认）。
- Docker Compose：`API_KEY_AUTH_ENABLED=false`（docker-compose.yml 覆盖）。

开放路径：

```text
API_KEY_OPEN_PATHS=/healthz,/readyz,/metrics,/docs,/openapi.json,/api/approvals/by-token
```

除开放路径外，请求必须携带：

```text
Authorization: Bearer <api_key>
```

## 开放路径匹配

开放路径会做 path normalize，并使用边界感知匹配：

- `/healthz` 匹配 `/healthz` 和 `/healthz/...`。
- 不匹配恶意 path traversal 后的其他业务路径。
- 配置项必须以 `/` 开头，否则会被忽略并记录 warning。

## 初始种子 key

`API_KEY_INITIAL_SEED` 可作为 bootstrap key。当数据库中尚未创建 key 或需要初始化管理 key 时，可用该 seed 访问 API key 创建接口。

seed key 不写入 `api_keys` 表，middleware 验证通过后设置：

```json
{
  "key_id": "apik_initial",
  "description": "initial-seed",
  "created_by": "system"
}
```

## API key 生命周期

### 创建

```http
POST /api/api-keys
```

创建响应会返回 raw key。raw key 只返回一次，不入库。数据库保存 SHA-256 hash、描述、创建者、过期时间、撤销状态。

### 列表

```http
GET /api/api-keys
```

只返回 metadata，不返回 raw key。

### 撤销

```http
DELETE /api/api-keys/{key_id}
```

撤销后该 key 不再通过验证。

## WebSocket 鉴权

WebSocket 无法使用常规 Authorization header 时，使用 query 参数：

```text
/api/ws/incidents/{incident_id}?token=<api_key>
```

启用鉴权且 token 缺失或无效时，连接关闭码为 `4001`。

## 本地开发

本地 demo 如需临时关闭鉴权：

```bash
API_KEY_AUTH_ENABLED=false
```

这只适合本地开发。共享环境应使用 seed key 创建正式 key，并保持鉴权开启。
