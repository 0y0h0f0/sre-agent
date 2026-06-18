# 通知、邮件、评论协作与操作员交互技术深挖

**最后更新：** 2026-06-17

本文补充 [Celery 与异步任务](../01-backend/celery-and-jobs.md)、[React 控制台](../06-frontend/react-console.md)、[前端控制台与实时更新技术深挖](frontend-realtime-console-deep-dive.md)、[护栏与审批技术深挖](guardrail-approval-deep-dive.md)、[认证、API Key、审计与安全边界技术深挖](auth-api-key-audit-security-deep-dive.md) 和 [数据模型、迁移与持久化技术深挖](data-model-migrations-persistence-deep-dive.md)。它解释当前通知、邮件、email token、评论、证据标注、WebSocket 实时事件、浏览器通知和审计如何组合成操作员交互面。

## 一句话模型

操作员交互面由三条异步链路和两条 REST 协作链路组成：

```text
邮件通知
  -> EmailLog 持久化
  -> Celery send_email_notification
  -> SMTP / skipped / retryable failed

实时更新
  -> worker node tracer
  -> Redis Pub/Sub incident:{incident_id}
  -> WebSocket
  -> TanStack Query invalidation

浏览器通知
  -> 前端发现新的 waiting approval
  -> Notification API / service worker showNotification
  -> 点击跳转到 /approvals/{approval_id}

评论与标注
  -> comments / evidence annotations REST API
  -> DB 持久化
  -> audit log
  -> 前端轮询展示
```

关键边界：

- 邮件通知是 best-effort，不是诊断主链路的硬依赖。
- `EmailLog` 是发送状态审计记录，不是业务动作权限来源。
- L2 可以通过 email token 批准或拒绝；L3 不能通过 email token 批准，必须回到 Web 控制台做二次确认。
- WebSocket 事件只用于刷新体验，不能作为状态 source of truth。
- 浏览器通知由前端根据 waiting approval 本地触发，不是服务端 push subscription。
- 评论和证据标注写 audit；comment 本身可以删除，audit 仍保留创建记录。

## 代码入口

| 主题 | 代码入口 | 说明 |
|------|----------|------|
| 邮件 composition / SMTP | `apps/api/services/email_service.py` | email content、template、SMTP、queue/send 状态 |
| Email log repository | `packages/db/repositories/email_logs.py` | `queued/sent/failed/skipped` 状态和 related ids |
| Worker 邮件 task | `apps/worker/tasks.py` | `enqueue_email_notification_task()`、`send_email_notification`、daily summary |
| 邮件模板 | `templates/email/*.html` | incident alert、diagnosis complete、approval request、report、daily summary |
| 审批 token | `apps/api/services/approval_service.py`、`apps/api/routers/approvals.py` | token 生成、确认页、approve/reject by token |
| 评论 API | `apps/api/routers/comments.py`、`apps/api/services/comment_service.py` | incident comments、evidence annotations、audit |
| 评论/标注 repository | `packages/db/repositories/comments.py`、`packages/db/repositories/evidence_annotations.py` | `cmt_` / `ean_` 持久化 |
| WebSocket publisher | `apps/api/ws/publisher.py` | Redis Pub/Sub event 发布，失败只 warning |
| WebSocket router | `apps/api/ws/router.py` | ticket 校验、订阅 `incident:{incident_id}`、转发 JSON |
| 前端 API client | `apps/web/src/api.ts` | comments、annotations、audit、batch approval、WS ticket |
| 前端页面 | `apps/web/src/App.tsx` | WebSocket hook、ApprovalNotificationControl、CommentSection、AuditSection |
| Service worker | `apps/web/public/sw.js` | app shell cache、notification click 导航 |

## 通知类型

当前 `EmailNotificationService.compose()` 支持 5 类通知：

| Type | 触发 | 内容 | 关联字段 |
|------|------|------|----------|
| `new_incident` | `AlertService.create_alert()` 创建 incident/run 并入队诊断后 | incident severity、service、fingerprint、incident URL | `related_incident_id` |
| `diagnosis_complete` | worker run 成功或 resume 成功后 | root cause、前 5 条 evidence、incident/run/report URL | `related_incident_id`、`related_agent_run_id` |
| `approval_request` | worker 进入 waiting approval 后 | action、target、risk、reason、approval URL；L2 带 direct email token link | `related_incident_id`、`related_agent_run_id`、`related_approval_id` |
| `incident_report` | worker 生成 report 或手动 regenerate 后 | report markdown 和 report URL | `related_incident_id`、`related_agent_run_id`、`related_report_id` |
| `daily_summary` | Celery Beat 每日调度 | 按 `NOTIFICATION_TIMEZONE` 的当天 incident 列表 | 无单个 incident 关联 |

API 和 worker 都把通知视为辅助链路：

- `AlertService` 在 incident/run 和 task id 都提交后才入队 `new_incident`；通知入队失败会被吞掉，不影响告警响应。
- `ReportService.regenerate()` 在新 report version 提交后入队 `incident_report`；失败不影响 report regenerate 响应。
- Worker 的 `_enqueue_notification_event()` 捕获异常并记录 error，不让邮件故障导致诊断失败。

## EmailLog 生命周期

`EmailLog` 是邮件发送状态审计：

```text
queue_event()
  -> compose(notification_type, payload)
  -> email_logs.create(status=queued)
  -> commit
  -> Celery send_email_notification(email_log_id, type, payload)
  -> send_queued_event()
       -> recompute content
       -> send_sync()
       -> sent / failed / skipped
       -> commit
```

状态含义：

| Status | 何时写入 |
|--------|----------|
| `queued` | email log row 已创建，等待发送 task |
| `sent` | SMTP provider 接受或发送成功；保存 `provider_message_id` 和 `sent_at` |
| `failed` | 发送失败且可重试，或 Celery enqueue 失败 |
| `skipped` | 不可重试前置条件缺失，例如无 recipients、无 `SMTP_HOST`、无 `SMTP_FROM` |

实现细节：

- `enqueue_email_notification_task()` 先写 `EmailLog`，再调用 Celery `send_email_notification.delay()`。
- 如果 Celery 入队失败，会把同一 `EmailLog` 标记为 failed，并重新抛出。
- `send_email_notification` 最多重试 3 次，重试时更新同一 `EmailLog` 的 attempts 和 last_error。
- `send_daily_incident_summary` 在 retry 时复用同一个 `email_log_id`，避免重复创建 summary log。
- Worker 在发送 diagnosis/approval/report 通知前用 `EmailLogRepository.exists_for_event()` 做 best-effort 去重；这是代码级查询，不是数据库唯一约束。

## SMTP 与模板

SMTP 配置来自 `Settings`：

| 配置 | 用途 |
|------|------|
| `SMTP_HOST` / `SMTP_PORT` | SMTP 服务器 |
| `SMTP_TLS_MODE` | `auto`、`starttls`、`tls`、`none` |
| `SMTP_TIMEOUT_SECONDS` | 发送超时 |
| `SMTP_USER` / `SMTP_PASSWORD` | 可选 SMTP 认证 |
| `SMTP_FROM` | 发件人 |
| `SRE_EMAIL_LIST` | 全局收件人，逗号或分号分隔 |
| `WEB_BASE_URL` | 邮件中的前端链接 base URL |
| `NOTIFICATION_TIMEZONE` | daily summary 窗口和 Beat timezone |

`SMTP_TLS_MODE=auto` 时：

- 端口 465 使用 implicit TLS。
- 端口 587 使用 STARTTLS。

`approval_request` 还会尝试读取 `ApprovalGroupRepository.find_by_service()`，把匹配审批组成员追加到全局收件人列表。审批组解析失败只记录 warning，不阻断邮件 composition。

## Approval Email Token

邮件审批 token 有两个生成入口：

1. `EmailNotificationService._approval_request()` 对非 L3 approval 自动生成或复用有效 token。
2. `POST /api/approvals/{approval_id}/email-token` 手动生成 24 小时 token。

L2 email flow：

```text
approval_request email
  -> /api/approvals/by-token/{token}/approve
  -> GET confirmation page
  -> browser POST /api/approvals/by-token/{token}/approve
  -> ApprovalService.approve_by_token()
  -> approve()
  -> audit
  -> commit decision
  -> maybe enqueue resume
  -> clear token
```

Reject flow 与 approve 相同，但调用 `reject_by_token()`。

安全边界：

- `API_KEY_OPEN_PATHS` 默认开放 `/api/approvals/by-token`，使邮件链接可不带 API key 使用。
- `POST /api/approvals/{approval_id}/email-token` 不在开放路径中；auth enabled 时仍受 API key auth 保护。
- token 成功使用后清空，二次使用返回 not found。
- token 过期时清空并返回业务校验错误。
- HTML confirmation page 的 GET 是安全展示，真正决策由 POST 发起。
- POST 支持 `redirect` query，但 `_validate_redirect()` 只允许相对路径，防止 open redirect。

## L3 与 Email Token

L3 approval 需要：

```text
risk_ack == true
confirm_action_type == action.type
confirm_target == (action.target or "")
```

Email token flow 没有这些二次确认字段，因此当前规则是：

- L3 approval email subject 使用 `[CONFIRM]`。
- L3 邮件内容引导打开 Web 控制台。
- `approve_by_token()` 遇到 L3 直接返回 `ValidationAppError`。
- L3 reject 可通过普通 reject path；但 L3 approve 必须走 Web UI 或 API 提供完整字段。

这条边界不能放松。Email one-click approve 只能覆盖不需要二次确认的审批。

## 评论与证据标注

评论 API：

| Endpoint | 行为 |
|----------|------|
| `POST /api/incidents/{incident_id}/comments` | 创建 incident comment；incident 不存在返回 404；写 audit `comment_add` |
| `GET /api/incidents/{incident_id}/comments` | 按 `created_at` 升序列出 comments |
| `DELETE /api/comments/{comment_id}` | 删除 comment；当前不写 audit |

评论 schema：

- `author`: 1-128 字符。
- `content`: 1-5000 字符。
- `parent_comment_id`: 可选，用于 threaded comments。
- `mentioned_users`: 最多 20 个；当前由客户端传入，后端不解析 `@handle` 文本。

证据标注 API：

| Endpoint | 行为 |
|----------|------|
| `POST /api/evidence/{evidence_id}/annotations` | 创建 evidence annotation；evidence 不存在返回 404；写 audit `evidence_annotate` |
| `GET /api/evidence/{evidence_id}/annotations` | 按 `created_at` 升序列出 annotations |

前端当前在 incident detail 展示 `CommentSection`，每 15 秒轮询 comments，新增评论后失效 `['incident-comments', incidentId]`。`api.ts` 已有 evidence annotation client 方法，测试覆盖 GET/POST；主 incident detail 页面当前重点展示评论和 audit。

## Audit 串联

操作员交互相关 audit 写入：

| 动作 | Audit action | 写入者 |
|------|--------------|--------|
| 单个 approve/reject | `approve` / `reject` | `ApprovalService` |
| batch approve/reject | `approve` / `reject`，每个 approval 一条 | `ApprovalService.batch_decide()` |
| comment create | `comment_add` | `CommentService.create_comment()` |
| evidence annotation create | `evidence_annotate` | `CommentService.create_annotation()` |
| feedback/NFA/root cause/action correction | 对应 feedback action | `FeedbackService` |
| config publish/rollback/revoke | `config.*` | `ConfigPublisher` |
| discovery/poll | `discovery.*` / poll audit | worker task |
| M9 incident diff | `runbook.amendment_draft.created` | `RunbookService` |

`GET /api/incidents/{incident_id}/audit` 读取 incident-scoped audit。前端 `AuditSection` 使用 30 秒 stale time，只展示 actor、action、resource 和时间，不提供修改或删除入口。

## WebSocket 实时更新

实时链路：

```text
Agent node starts/finishes
  -> node_tracer writes agent_run_nodes
  -> publish_node_event(...)
  -> Redis channel incident:{incident_id}
  -> WebSocket router forwards JSON
  -> App.tsx useWebSocket receives event
  -> invalidate agent-run and incident queries
```

当前 `apps/api/ws/publisher.py` 支持：

- `node_update`
- `approval_update`
- 自定义 `publish_event(...)`

当前确认的主动发布路径是 worker node tracer 的 `node_update`。前端也容忍 `approval_update` 和 `incident_update`，收到这些事件时同样失效相关 query。客户端必须把 REST API/DB 结果作为最终状态来源，不能把 WebSocket payload 当成持久状态。

失败行为：

- Redis publish 失败只记录 warning，不让 worker 失败。
- WebSocket JSON parse 失败会忽略该消息。
- WebSocket 断开后前端 5 秒重连。
- Auth enabled 时 WebSocket 需要短期 ticket；ticket 细节见 [认证、API Key、审计与安全边界技术深挖](auth-api-key-audit-security-deep-dive.md)。

## 浏览器通知

浏览器通知不是服务端 push。当前行为由前端 `ApprovalNotificationControl` 决定：

1. `/approvals` 页面查询 waiting approvals。
2. 第一次渲染时记录已有 approval ids，避免打开页面立即弹历史通知。
3. 用户点击“启用通知”后调用 `Notification.requestPermission()`。
4. permission 为 `granted` 后，后续新出现的 waiting approval 会触发通知。
5. 有 service worker registration 时使用 `registration.showNotification()`；否则使用 `new Notification()`。
6. `/sw.js` 的 `notificationclick` 导航到通知 `data.url`，默认 `/approvals`。

Service worker 当前还做 app shell cache：

- 缓存 `/`、`/index.html`、`/manifest.webmanifest`、`/icon.svg`。
- navigation 请求失败时回退 `/index.html`。
- 只处理同源 GET。
- 不处理后台 push subscription，不发送 API mutation。

## 操作员交互读写路径

| 交互 | 主数据 | 实时/通知 | 审计 |
|------|--------|-----------|------|
| 新告警 | `incidents`、`agent_runs` | email `new_incident` best-effort | 无专门 audit，alert payload 持久化 |
| 等待审批 | `actions`、`approvals` | email `approval_request`、frontend waiting polling | human_approval node trace/tool/evidence 链路 |
| 批准/拒绝 | `approvals`、`actions` | resume Celery、frontend mutation invalidation | `approve` / `reject` |
| 诊断完成 | `agent_runs`、`incident_reports` | email `diagnosis_complete` / `incident_report` | node/tool/evidence/report 持久化 |
| 评论 | `incident_comments` | comments polling | `comment_add` |
| 证据标注 | `evidence_annotations` | annotation API | `evidence_annotate` |
| 浏览器通知 | 前端内存 Set | Notification API / SW click | 无后端 audit |

## 常见排障

| 问题 | 首看 | 判断 |
|------|------|------|
| 没有邮件 | `email_log`、worker logs、SMTP settings | 是否 queued、skipped、failed；`SRE_EMAIL_LIST`、`SMTP_HOST`、`SMTP_FROM` 是否配置 |
| 邮件重复 | `email_log` related ids | worker 是否多次触发；去重是查询式，不是唯一约束 |
| 邮件 task 重试多次 | `email_log.attempts`、`last_error` | 是否 retryable SMTP error，是否达到 3 次 |
| L2 email token 失败 | `approvals.email_token`、`email_token_expires_at` | token 是否已使用、过期、approval 是否已决定 |
| L3 邮件批准失败 | action risk level | 这是预期行为，必须用 Web UI/API 完整二次确认 |
| HTML token POST 跳转异常 | `redirect` query | 只能是相对路径，绝对 URL 会被拒绝 |
| 评论不显示 | comments API、frontend query key | 15 秒轮询或新增评论后失效是否触发 |
| 标注找不到证据 | `evidence_items` | annotation 创建要求 evidence 已持久化 |
| WebSocket 没实时更新 | Redis Pub/Sub、ticket、connection state | REST 轮询仍应兜底；事件不是 source of truth |
| 浏览器通知不弹 | Notification permission、service worker registration | 首次渲染不弹历史通知；用户拒绝后无法弹 |

## 新增通知或协作能力 checklist

1. 判断它是业务状态、邮件通知、实时事件还是浏览器本地通知。
2. 业务状态必须先持久化，再触发通知或 WebSocket。
3. 邮件通知要新增 `notification_type`、template、`EmailContent`、related id 和测试。
4. 邮件失败不应阻断诊断主链路，除非用户明确请求同步发送语义。
5. 需要去重时，明确是数据库唯一约束还是 `exists_for_event()` 查询式去重。
6. 涉及 approval 时，保持 L3 二次确认只能在完整 UI/API 中完成。
7. 新开放 email token endpoint 必须校验 open redirect、token expiry 和一次性使用。
8. 新评论/标注写路径要写 audit，并校验 parent/evidence/incident 是否存在。
9. 新 WebSocket event 要让客户端容忍未知 event，并继续以 REST query 为最终状态。
10. 更新 `docs/01-backend/api-reference.md`、前端文档、Celery 文档、状态/ID 文档和测试说明。

## 验证入口

Codex 按项目约束不直接运行 `pytest`、前端测试、Playwright 或完整测试套件。涉及通知/协作变更时，建议由开发者本地运行：

```bash
pytest tests/unit/test_email_notifications.py -v
pytest tests/integration/test_email_smtp_delivery.py -v
pytest tests/integration/test_phase6_collaboration.py -v
pytest tests/integration/test_approval_api.py -v
```

涉及前端交互时再运行：

```bash
cd apps/web
npm run test:coverage
```

手动真实 SMTP 验证仍属于 manual path：

```bash
pytest tests/manual/test_real_email_delivery.py -v
pytest tests/manual/test_smtp_connectivity.py -v
```
