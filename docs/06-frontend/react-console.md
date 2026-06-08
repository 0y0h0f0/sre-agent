# React 控制台

## 技术栈

- React 19。
- TypeScript。
- Vite。
- React Router。
- TanStack Query。
- lucide-react icons。
- Vitest + React Testing Library。
- Playwright。

前端路径：

```text
apps/web/
```

## 页面路由

当前 `App.tsx` 定义：

| 路径 | 页面 |
| --- | --- |
| `/` | 重定向到 `/incidents` |
| `/incidents` | 事故列表 |
| `/incidents/:incidentId` | 事故详情 |
| `/incidents/:incidentId/report` | 事故报告 |
| `/agent-runs/:agentRunId` | Agent run 详情 |
| `/approvals` | 审批列表 |
| `/approvals/:approvalId` | 审批详情 |
| `*` | 404（NotFoundPage） |

## API client

`src/api.ts` 提供 typed API client。

核心函数：

- `listIncidents`
- `getIncident`
- `triggerDiagnosis`
- `listIncidentRuns`
- `getAgentRun`
- `listApprovals`
- `listIncidentApprovals`
- `getApproval`
- `getAction`
- `approveApproval`
- `rejectApproval`
- `getIncidentReport`
- `regenerateIncidentReport`
- `markIncidentNFA`
- `correctIncidentRootCause`
- `correctIncidentAction`
- `getCorrelatedIncidents`
- `listIncidentFeedback`
- `listIncidentComments`
- `createComment`
- `deleteComment`
- `listEvidenceAnnotations`
- `createEvidenceAnnotation`
- `listIncidentAudit`
- `batchDecideApprovals`

`apiRequest()` 自动生成 `X-Request-Id`。错误响应会转换为 `ApiError`，保留：

- HTTP status。
- error code。
- request id。
- details。

## API base URL

前端读取：

```text
VITE_API_BASE_URL
```

未设置时使用当前 origin。

## 鉴权

WebSocket 使用 localStorage 中的：

```text
sre_api_key
```

构造连接：

```text
/api/ws/incidents/{incident_id}?token=<api_key>
```

HTTP API client 使用同一个 `sre_api_key` localStorage key；存在时发送 `Authorization: Bearer <api_key>`，不存在时不发送鉴权头。本地 Docker demo 默认关闭 API key 鉴权，生产式使用应显式配置 key。

侧边栏包含 `Authentication` 面板，支持：

- 粘贴已有 raw API key 并保存到当前浏览器。
- 输入 bootstrap seed 或已有管理员 API key 调用 `POST /api/api-keys` 生成新 key。
- 生成成功后自动保存返回的 `raw_key`，并刷新当前页面查询。
- 清除当前浏览器保存的 key。

当 API 返回 `UNAUTHORIZED` 时，错误状态会提示用户在侧边栏认证面板设置或生成 API key。

## 实时更新

`useWebSocket()` 订阅事故事件：

- 状态：`disabled`、`connecting`、`open`、`closed`、`error`。
- 断开后 5 秒重连。
- 最近事件保留 40 条。
- 收到事件后可触发 query invalidation 或 UI 更新。

Worker 通过 Redis pub/sub 发布节点事件。

## 轮询

对于 live 状态，页面使用 TanStack Query refetch interval：

```text
open
diagnosing
waiting_approval
queued
running
executing
```

事故列表和事故详情通常 5 秒轮询。

## UI 状态要求

每个页面应覆盖：

- loading。
- empty。
- error。
- retry。
- active run polling。
- approval conflict。
- L3 second confirmation。
- cache/token/compression display。

## 事故详情操作

`/incidents/:incidentId` 页面顶部操作区包含：

- `Agent 运行`：跳转到最新 agent run 详情。
- `重新诊断`：弹出确认后调用 `triggerDiagnosis(incidentId, { force: true, reason: "manual rerun from UI" })`。
- `报告`：跳转到事故报告页。
- `标记无效`：调用 NFA API。

`重新诊断` 会通过 `POST /api/incidents/{incident_id}/diagnose` 创建新的 agent run，不覆盖旧 run，也不删除旧 run。成功后前端应刷新 incident、incident runs 和 incident approvals 查询。失败时在事故详情页显示错误 callout。

`/agent-runs/:agentRunId` 页面保持 run 追踪视图语义，只提供刷新当前 run 数据，不提供原地 rerun 当前 run 的主操作。

## L3 审批 UI

L3 动作必须显示二次确认输入：

- risk acknowledgement checkbox。
- confirm action type。
- confirm target。

提交 payload 必须包含：

```json
{
  "risk_ack": true,
  "confirm_action_type": "<action.type>",
  "confirm_target": "<action.target>"
}
```

## 前端命令

```bash
cd apps/web
npm install
npm run dev
npm run build
npm run test
npm run test:coverage
npm run test:e2e
```

## 测试配置

Vitest：

- jsdom environment。
- `src/**/*.{test,spec}.{ts,tsx}`。
- 排除 `src/e2e`、`node_modules`、`dist`。
- coverage reporter：text、html。
- thresholds：statements/branches/functions/lines 均为 80。

Playwright：

- testDir：`src/e2e`。
- webServer：`npm run dev -- --port 5173`。
- baseURL：`http://127.0.0.1:5173`。
- 可复用已有 dev server。
