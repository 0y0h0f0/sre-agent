# 前端控制台与实时更新技术深挖

**最后更新：** 2026-06-18

本文沿当前代码路径解释 React 控制台如何读取事件、Agent run、审批、报告、评论和审计数据，以及它如何用 TanStack Query、轮询、WebSocket ticket 和 Redis Pub/Sub 组合出实时诊断体验。它补充 [React 控制台](../06-frontend/react-console.md)：前端专题文档说明页面行为，本文说明运行时数据流、刷新策略和后端实时链路。需要聚焦邮件通知、approval email token、浏览器通知、comments/annotations 和操作员审计串联时，见 [通知、邮件、评论协作与操作员交互技术深挖](notifications-collaboration-operator-interaction-deep-dive.md)。需要聚焦 NFA、根因修正、相关事件和 feedback API helper 的后端学习边界时，见 [反馈、NFA、关联事件与持续学习技术深挖](feedback-nfa-correlation-continuous-learning-deep-dive.md)。

## 阅读目标

读完本文应能回答：

- 前端从哪里保存 API key，如何给 REST 请求加 `X-Request-Id` 和 bearer token。
- 每个页面的 Query key、轮询条件、mutation 成功后的失效范围是什么。
- Agent Run 页面为什么同时使用 5 秒轮询和 WebSocket 事件。
- WebSocket ticket 如何避免把长期 API key 放进 URL。
- L3 二次确认字段在哪里采集，批量审批如何避免绕过 L3 单独确认。
- `AgentRunDetail.state`、`nodes`、`tool_calls` 分别如何进入运行可视化。
- 页面错误态、401 提示、报告 404 空态和通知 service worker 的边界在哪里。

## 代码入口

| 主题 | 当前入口 |
|------|----------|
| React 根节点 | `apps/web/src/main.tsx` |
| 页面路由和组件 | `apps/web/src/App.tsx` |
| API client 和类型 | `apps/web/src/api.ts` |
| 全局样式 | `apps/web/src/styles.css` |
| 浏览器通知 service worker | `apps/web/public/sw.js` |
| WebSocket HTTP ticket API | `apps/api/ws/router.py` |
| WebSocket ticket 签发和校验 | `apps/api/services/ws_ticket_service.py` |
| Redis Pub/Sub 发布 | `apps/api/ws/publisher.py` |
| Agent run response schema | `apps/api/schemas/agent_runs.py` |
| Agent run detail service | `apps/api/services/agent_run_service.py` |
| 前端页面测试 | `apps/web/src/App.test.tsx` |
| API client 测试 | `apps/web/src/api.test.ts` |
| Playwright smoke | `apps/web/src/e2e/smoke.spec.ts` |

## 核心对象

| 对象 | 前端类型/组件 | 后端来源 | 用途 |
|------|---------------|----------|------|
| Incident list item | `IncidentListItem` | `GET /api/incidents` | 事件列表、筛选和 live 状态轮询判断。 |
| Incident detail | `IncidentDetail` | `GET /api/incidents/{incident_id}` | 诊断摘要、证据、推荐动作、详情页主记录。 |
| Agent run | `AgentRunDetail` | `GET /api/agent-runs/{agent_run_id}` | 节点轨迹、工具调用、checkpoint、运行状态和 state 展示快照。 |
| Agent run node | `AgentRunNode` | `agent_run_nodes` 汇总 | `RunProgress`、`RunTimeline` 和 fallback 实时日志。 |
| Tool call summary | `ToolCallSummary` | `tool_calls` 汇总 | 工具调用列表、信号泳道、cache hit/miss 展示。 |
| Approval | `ApprovalItem` | approval API | 审批列表、待审批区块、审批弹窗。 |
| Action detail | `ActionDetail` | `GET /api/actions/{action_id}` | 审批弹窗中的目标、参数和执行事实。 |
| Report | `IncidentReport` | report API | 报告页版本、根因、影响、时间线、后续项和证据引用。 |
| Comment/Audit | `CommentItem` / `AuditLogItem` | collaboration/audit API | 事件详情页协作和审计区域。 |
| WebSocket event | `WsEvent` | Redis channel 转发 | 节点、审批、事件更新的实时提示和 query invalidation。 |

## 总链路

```text
Browser
  -> localStorage[sre_api_key]
  -> api.ts apiRequest()
       -> X-Request-Id: req_<time>_<random>
       -> Authorization: Bearer <api key>
       -> REST API
       -> TanStack Query cache
       -> page components

Agent Run page
  -> GET /api/agent-runs/{run_id}
  -> if run live: POST /api/ws/incidents/{incident_id}/ticket
  -> WebSocket /api/ws/incidents/{incident_id}?ticket=<short_lived_ticket>
       -> Redis subscribe incident:{incident_id}
       -> node_update / approval_update / incident_update
       -> invalidate agent-run and incident query
```

前端不是执行面。它只展示后端状态、触发明确的 HTTP mutation、收集审批输入。风险等级、L3 校验、审批恢复、执行器选择和真实 remediation 权限都在后端完成。

## 1. App Bootstrap

`main.tsx` 做三件事：

- 创建默认 `QueryClient`。
- 用 `BrowserRouter` 挂载 `App`。
- 只在生产构建中自动注册 `/sw.js`。

审批通知按钮也会在用户授权通知后注册同一个 service worker。开发模式不会自动安装 service worker，避免 Vite 热更新期间出现缓存干扰。

`App.tsx` 是当前前端的主要文件。它包含路由、页面组件、通用 loading/empty/error 组件、WebSocket hook、审批弹窗、运行可视化和格式化 helpers。新增页面时优先沿用这个文件内已有模式，再视规模拆分组件。

## 2. API Client Contract

`api.ts` 是前端唯一的 HTTP client 层。组件不直接拼 ad hoc `fetch`。

固定行为如下：

| 行为 | 当前实现 |
|------|----------|
| API base URL | `VITE_API_BASE_URL` 存在时用绝对 URL；否则用当前 origin 和相对路径。 |
| Request ID | 每个请求加 `X-Request-Id: req_<base36 timestamp>_<random>`。 |
| API key 存储 | `localStorage["sre_api_key"]`。 |
| 认证头 | 有 API key 时加 `Authorization: Bearer <key>`。 |
| Bootstrap 创建 key | `createApiKey(payload, authToken)` 使用显式传入 token，不使用已保存 key。 |
| 错误解析 | 标准 `{error:{code,message,request_id,details}}` 转成 `ApiError`。 |
| 非标准错误 | 使用 HTTP status 和响应头 `X-Request-Id` 兜底。 |
| 分页兼容 | `normalizePaginated()` 同时兼容 `{items,total,page,page_size}` 和旧数组响应。 |

请求 ID 前缀是 `req_`。这与后端统一 request ID 语义一致，页面错误态会显示 `ApiError.requestId`。

### API Key Panel

左侧认证面板有两个路径：

- 手动保存已有 API key：写入 `localStorage["sre_api_key"]`，然后使当前 QueryClient 的查询失效并 refetch active queries。
- 用 bootstrap/admin token 创建 Web key：调用 `POST /api/api-keys`，默认 payload 包含 `description="本地 Web 密钥"`、`expires_in_days=90`、`scopes=["api_key:admin"]`、`roles=["operator"]`，成功后保存返回的 `raw_key`。

这个面板只是本地开发和演示便利入口。scope enforcement 仍由后端 middleware/dependency 决定。

## 3. Query and Mutation Strategy

TanStack Query 管理所有 server state。组件根据状态展示 loading、empty、error，并在 mutation 成功后失效相关 query。

| 页面/组件 | Query key | 数据源 | 刷新/失效策略 |
|-----------|-----------|--------|----------------|
| 事件列表 | `['incidents', filters]` | `GET /api/incidents` | 返回列表中存在 live incident 时 5 秒轮询。 |
| 事件详情 | `['incident', incidentId]` | `GET /api/incidents/{id}` | incident live 时 5 秒轮询。 |
| 事件 runs | `['incident-runs', incidentId]` | `GET /api/incidents/{id}/runs` | 重新诊断成功后失效。 |
| 事件审批 | `['incident-approvals', incidentId]` | `GET /api/incidents/{id}/approvals` | 待审批区块固定 5 秒轮询；审批 mutation 后失效。 |
| 相关事件 | `['correlated-incidents', incidentId]` | `GET /api/incidents/{id}/correlated` | `staleTime=60s`。 |
| Agent run | `['agent-run', agentRunId]` | `GET /api/agent-runs/{id}` | run live 时 5 秒轮询；WebSocket 事件触发失效。 |
| 审批列表 | `['approvals', status]` | `GET /api/approvals?status=...` | `status=waiting` 时 5 秒轮询。 |
| 单个审批 | `['approval', approvalId]` | `GET /api/approvals/{id}` | 深链接打开时查询。 |
| Action 详情 | `['action', actionId]` | `GET /api/actions/{id}` | 审批弹窗打开时查询。 |
| 事件报告 | `['incident-report', incidentId]` | report API | 重新生成成功后失效。 |
| 评论 | `['incident-comments', incidentId]` | comments API | 15 秒轮询；新增评论后失效。 |
| 审计 | `['incident-audit', incidentId]` | audit API | `staleTime=30s`。 |

`LIVE_STATUSES` 当前包括：

```text
open, diagnosing, waiting_approval, queued, running, executing
```

这些状态只影响前端是否轮询，不代表后端状态机的完整枚举。

## 4. Routes and Page Ownership

| 路由 | 组件职责 | 主要 mutation |
|------|----------|---------------|
| `/` | 重定向到 `/incidents` | 无 |
| `/incidents` | 事件筛选和列表 | 无 |
| `/incidents/:incidentId` | 事件详情、证据、动作、审批摘要、评论、审计 | 重新诊断、NFA 标记、根因修正、评论创建 |
| `/agent-runs/:agentRunId` | 运行进度、节点时间线、实时日志、可视化、工具调用、上下文摘要 | 无直接执行 mutation |
| `/approvals` | 审批队列、状态筛选、批量决定、通知按钮 | 批量批准/拒绝 |
| `/approvals/:approvalId` | 审批队列并打开指定审批 | 单个批准/拒绝 |
| `/incidents/:incidentId/report` | 报告读取和重新生成 | 报告重新生成 |

侧边栏只暴露事件和审批两个主入口。Agent run 和 report 从事件详情进入，审批详情支持外部链接直接打开。

## 5. Incident Pages

### Incident List

`IncidentsPage` 从 URL query 读取 `status`、`service`、`severity`，固定请求 `page_size=50`。用户提交筛选表单后更新 URLSearchParams，因此筛选条件可复制。

列表展示：

- service
- alert name
- severity
- status
- root cause summary 或等待诊断提示
- updated timestamp

如果返回列表中有 live incident，就启用 5 秒轮询。没有 live incident 时停止自动轮询，减少本地 demo 和生产控制台的空转请求。

### Incident Detail

`IncidentDetailPage` 聚合多个数据源：

- incident 主记录
- runs
- approvals
- correlated incidents
- comments
- audit entries

主要写操作：

| 操作 | API | 成功后 |
|------|-----|--------|
| 重新诊断 | `POST /api/incidents/{id}/diagnose`，payload `{force:true, reason:"manual rerun from UI"}` | 失效 incident、runs、approvals。 |
| 标记 NFA | `POST /api/incidents/{id}/nfa` | 失效 incident。 |
| 修正根因 | `PATCH /api/incidents/{id}/root-cause` | 失效 incident，退出编辑态。 |
| 新增评论 | `POST /api/incidents/{id}/comments` | 失效 comments。 |

事件详情页展示推荐动作，但不执行动作。动作执行仍由后端 approval/resume/executor 链路控制。

## 6. Agent Run Page

`AgentRunPage` 展示的是一次 LangGraph run 的可审计快照。数据分三类：

| 数据 | 来源 | 前端用途 |
|------|------|----------|
| `run.status`、checkpoint 字段 | `agent_runs` | 顶部状态、检查点指标和轮询判断。 |
| `run.nodes` | `agent_run_nodes` | 进度条、节点轨迹、fallback 实时日志。 |
| `run.tool_calls` | `tool_calls` | 工具调用列表、信号泳道、cache hit/miss。 |
| `run.state` | `agent_runs.state` 展示快照 | context/token 摘要、拓扑、假设和 evidence network 的补充数据。 |

`agent_runs.state` 不是 checkpoint source of truth。前端不能基于它恢复 LangGraph，也不能把它当作审批状态的唯一来源。

### Progress and Visualizations

`getRunProgress()` 优先从 `run.state` 中尝试读取预期节点顺序，例如 `graph_node_order`、`expected_nodes`、`workflow.nodes`。如果没有这些字段，就按后端返回的 node traces 排序。

可视化当前由前端派生：

- `RunProgress` 用 node status 计算完成数和当前节点。
- `RunTimeline` 展示每个节点 input/output summary。
- `LiveNodeLog` 优先展示 WebSocket `node_update`，没有实时事件时回退到当前 node trace。
- `SignalSwimlanes` 按 `tool_name`/`node_name` 把工具调用归类到 metrics/logs/traces/git/runbook/agent。
- `DependencyGraph` 优先读取 `state.service_topology` 或 `state.topology`，否则从 service 和信号来源生成简化图。
- `EvidenceNetwork` 读取 `ranked_hypotheses`、`hypotheses`、`root_cause`、`evidence_ids`，再补充 tool call IDs。
- `ContextSummary` 读取 `state.token_usage`、`state.context_budget` 和 `state.compression_events`。

这些可视化用于排障和讲解，不替代后端诊断判定。

## 7. WebSocket Realtime Path

### Browser Side

`useWebSocket(incidentId, enabled)` 的行为：

- enabled 为 false 或没有 incident id 时，状态置为 `disabled`，清空本地事件。
- 连接前调用 `buildIncidentWebSocketUrl()`。
- 如果浏览器保存了 API key，先调用 `createWebSocketTicket(incidentId)`。
- WebSocket URL 只带 `ticket`，不带长期 API key。
- `onmessage` JSON parse 成 `WsEvent`，最多保留最近 40 条事件。
- `node_update`、`approval_update`、`incident_update` 会使当前 `agent-run` 和 incident query 失效。
- 断开后 5 秒重连。

无 API key 时前端会连接不带 ticket 的 URL。后端是否接受由 `API_KEY_AUTH_ENABLED` 决定。

### Backend Side

后端实时链路如下：

```text
POST /api/ws/incidents/{incident_id}/ticket
  -> get_current_api_key
  -> WebSocketTicketService.issue()
       -> incident_id + key_id + exp + nonce
       -> HMAC-SHA256 signature
       -> ttl default 60 seconds

WebSocket /api/ws/incidents/{incident_id}?ticket=...
  -> if API_KEY_AUTH_ENABLED: verify ticket
  -> Redis subscribe incident:{incident_id}
  -> send {"type":"connected","incident_id":...}
  -> forward Redis messages as JSON

worker/API publisher
  -> publish_event(incident_id, event_type, payload)
  -> Redis publish incident:{incident_id}
```

`WebSocketTicketService` 在生产环境且 API key auth 开启时要求配置 `WEBSOCKET_TICKET_SECRET`。本地未配置时使用进程内随机 secret，适合 demo，但不适合多副本生产。

Pub/Sub 失败不会让 worker 崩溃。`publish_event()` 捕获异常并记录 warning，因此实时 UI 是增强路径，不是诊断执行的可靠性前提。

## 8. Approval UI

审批页面有两层交互：

- `ApprovalsPage`：列表、状态筛选、深链接打开弹窗、批量批准/拒绝、通知按钮。
- `ApprovalDialog`：读取 action detail，提交单个 approve/reject。

### Single Approval

单个审批弹窗规则：

| 场景 | 前端采集 | 后端最终校验 |
|------|----------|--------------|
| L2 approve | `approver`，可选 `comment` | approval service 校验状态和权限。 |
| L3 approve | `approver`、`risk_ack`、`confirm_action_type`、`confirm_target` | 后端要求 `risk_ack=true` 且确认字段匹配 action。 |
| reject | `approver`、必填 `comment` | 后端写 rejected 并触发 resume/replan 路径。 |

单个 approve/reject 成功后失效：

- `['approvals']`
- `['incident', approval.incident_id]`
- `['incident-approvals', approval.incident_id]`
- `['agent-run', approval.agent_run_id]`

### Batch Approval

批量审批发送到 `POST /api/approvals/batch`。

当前 UI 行为：

- waiting approval 才显示 checkbox。
- 选中项中包含 L3 时，批量批准按钮禁用，并显示 `L3 需单独确认`。
- 批量拒绝仍可用，payload 包含 `decision="reject"`、`approver="sre-batch"`、comment 和 `approval_ids`。

这是用户体验层的防护。最终安全边界仍在后端：L3 批准必须带二次确认字段，L4 不应进入审批。

## 9. Reports, Comments, Audit and Notifications

### Reports

`ReportPage` 读取 `GET /api/incidents/{incident_id}/report`：

- 404 显示空态，表示尚无报告版本。
- 其他错误显示标准 `ErrorState`。
- 点击生成/重新生成调用 `POST /api/incidents/{incident_id}/report/regenerate`。
- 成功后失效 `['incident-report', incidentId]`。

报告版本化由后端保证。前端只展示当前返回版本，不覆盖历史版本。

报告生成、再生成、latest-only API、通知和 incident/run lifecycle 的后端细节见 [报告生成、版本与事件生命周期技术深挖](report-generation-incident-lifecycle-deep-dive.md)。

### Comments and Audit

`CommentSection` 每 15 秒轮询 comments，新增评论后失效 comments query。评论内容支持 `@handle` 文本，但提及解析和持久化字段由后端返回值决定。

`AuditSection` 使用 `staleTime=30s` 读取 audit。它只展示 actor、action、resource 和时间，不提供修改或删除审计记录的入口。

### Browser Notifications

`ApprovalNotificationControl` 只对新出现的 waiting approval 发送浏览器通知：

- 第一次渲染时先记录已有 approval id，避免立即弹出历史通知。
- permission 不是 `granted` 时不弹通知。
- 已通知的 approval id 存在组件内存 `Set`，避免重复通知。
- 有 service worker registration 时用 `registration.showNotification()`；否则退回 `new Notification()`。
- `/sw.js` 处理 `notificationclick`，导航到通知携带的审批 URL，默认 `/approvals`。

Service worker 还缓存同源 GET app shell。它不会缓存非 GET 请求，也不会缓存跨 origin 请求。

## 10. Error and Empty States

通用状态组件在 `App.tsx`：

| 组件 | 用途 |
|------|------|
| `LoadingPage` | 页面级加载。 |
| `LoadingRows` | 表格/列表骨架。 |
| `EmptyState` | 无事件、无审批、无报告、无工具调用等空态。 |
| `ErrorState` | 显示错误消息、错误码、request id、401 API key 提示和重试按钮。 |

错误处理原则：

- HTTP client 优先解析标准错误信封。
- 页面不解析后端非标准错误字符串。
- 401 统一提示在左侧认证面板设置或生成 API key。
- 报告 404 是业务空态，不是页面崩溃。
- WebSocket JSON parse 失败会忽略该消息，不影响连接。

## 11. Safety Boundaries

前端必须保持以下边界：

- 不根据模型输出或页面字段决定最终风险等级。
- 不构造绕过 approval service 的执行请求。
- 不把长期 API key 放入 WebSocket URL。
- 不把 `agent_runs.state` 当作 checkpoint 或恢复源。
- 不把 L3 批量批准做成自动补确认字段。
- 不为 L4 或破坏性动作提供“强制执行”入口。
- 不把 service worker 用作离线执行或后台审批通道。

如果新增前端能力涉及外部写入、真实执行、审批、API key 或 M9 外部调用，应同步检查后端 guardrail、scope、audit 和测试，而不是只改 UI。

## 12. Test Coverage Map

当前前端测试入口：

| 文件 | 当前覆盖重点 |
|------|--------------|
| `apps/web/src/App.test.tsx` | 事件列表/详情、API key 面板、Agent run、WebSocket 事件、审批、L3 二次确认、批量 L3 禁用、通知、报告、评论、审计、404。 |
| `apps/web/src/api.test.ts` | bearer token、request id、API key 存储、bootstrap token、分页兼容、错误信封、L3 payload、helper endpoint。 |
| `apps/web/src/e2e/smoke.spec.ts` | 控制台主 smoke 流程。 |

按项目测试策略，Codex 不直接运行前端测试。需要本地验证时由用户执行：

```bash
cd apps/web
npm run test:coverage
npm run test:e2e
```

## 13. Debug Checklist

| 现象 | 先看 | 常见原因 |
|------|------|----------|
| 页面一直 401 | Network request、`localStorage["sre_api_key"]`、`ApiError.requestId` | 未保存 key、key 过期、scope 不足、auth 配置开启。 |
| 事件列表不自动刷新 | incident status、`LIVE_STATUSES`、query data | 所有事件已终态，前端停止轮询。 |
| Agent run 页面旧数据 | `agent-run` query、WebSocket connection pill、Network | run 已终态、ticket 签发失败、WebSocket 被认证拒绝、Redis Pub/Sub 不通。 |
| WebSocket 连接被关闭 | backend close reason、`API_KEY_AUTH_ENABLED`、ticket URL | 缺 ticket、ticket 过期、`WEBSOCKET_TICKET_SECRET` 不一致。 |
| 审批后 run 没恢复 | approvals 状态、agent-run query、worker logs | 后端仍有 waiting approval、resume task 未执行、worker 未在线。 |
| L3 批量批准不可点 | 选中项风险等级 | 当前 UI 设计为 L3 必须单独确认。 |
| 报告页显示空态 | report API status | 后端返回 404，说明该 incident 尚无报告版本。 |
| 浏览器通知不弹 | Notification permission、service worker registration | 用户未授权、浏览器不支持、首次渲染只记录已有 approvals。 |
