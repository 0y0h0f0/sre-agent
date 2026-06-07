# 运维手册

## 常用检查

API：

```bash
curl http://localhost:8000/healthz
curl http://localhost:8000/readyz
```

容器：

```bash
docker compose ps
docker compose logs -f api
docker compose logs -f worker
docker compose logs -f postgres
docker compose logs -f redis
```

数据库迁移：

```bash
alembic current
alembic upgrade head
```

## 告警没有生成事故

检查：

- API 是否可达。
- 请求是否有 `Content-Type: application/json`。
- 鉴权是否需要 `Authorization: Bearer <api_key>`。
- payload 是否包含或可归一化为 `fingerprint`、`service`、`severity`、`alert_name`、`starts_at`。
- `X-Request-Id` 对应 API logs。

## 告警返回 deduplicated

open incident fingerprint 唯一。若同 fingerprint 的 incident 仍处于：

- `open`
- `diagnosing`
- `waiting_approval`

则新告警会复用已有 incident，不新建。

## 诊断任务不运行

检查：

- `docker compose logs -f worker`。
- Redis broker 是否可达。
- `agent_runs.status` 是否为 `queued`、`running`、`waiting_approval` 或终态。
- 是否有 stale running run，等待 `TASK_ORPHAN_TIMEOUT_SECONDS` 后可重试。
- checkpointer 是否初始化失败。真实 PostgreSQL 下失败会 fail closed。

## 长时间 waiting approval

操作：

1. 打开 `/approvals?status=waiting`。
2. 查看 risk level。
3. L2 可直接填写 approver/comment 审批。
4. L3 必须确认 action type 和 target。
5. 审批后观察 worker 是否执行 resume task。

API：

```bash
curl http://localhost:8000/api/approvals?status=waiting
```

## L3 审批失败

常见原因：

- `risk_ack` 不是 true。
- `confirm_action_type` 与 action.type 不一致。
- `confirm_target` 与 action.target 不一致。
- approval 已经不是 waiting。

应通过 `GET /api/actions/{action_id}` 查看真实 action 字段后再提交。

## L4 动作被拒绝

这是预期行为。L4 不进入审批，不执行。报告应说明动作被阻断的原因。

## Runbook 搜索为空

检查：

- 是否执行过 `/api/runbooks/ingest`。
- Runbook Markdown front matter 是否可解析。
- service 和 incident_type filter 是否过窄。
- `runbook_chunks` 是否有数据。
- embedding provider 是否为 fake 或可用真实 provider。

## WebSocket 连接失败

检查：

- `API_KEY_AUTH_ENABLED` 是否开启。
- localStorage 中是否存在 `sre_api_key`。
- token 是否未撤销、未过期。
- Redis 是否可达。
- API logs 是否有 close code 4001。

## 邮件未发送

检查配置：

- `SMTP_HOST`
- `SMTP_PORT`
- `SMTP_TLS_MODE`
- `SMTP_USER`
- `SMTP_PASSWORD`
- `SMTP_FROM`
- `SRE_EMAIL_LIST`
- `WEB_BASE_URL`

检查表：

- `email_log.status`
- `email_log.attempts`
- `email_log.last_error`

手动 smoke：

```bash
RUN_REAL_EMAIL_TEST=true pytest tests/manual/test_smtp_connectivity.py -q
RUN_REAL_EMAIL_TEST=true pytest tests/manual/test_real_email_delivery.py -q
```

## 安全检查清单

变更前确认：

- 没有新增真实 executor 默认路径。
- K8s/DB 工具仍为只读。
- L3 二次确认测试仍通过。
- L4 direct reject 测试仍通过。
- FakeLLM 仍是 CI 默认。
- report regenerate 不覆盖旧版本。
- API error envelope 未破坏。
