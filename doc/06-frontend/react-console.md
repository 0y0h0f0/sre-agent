# React 控制台设计

## 技术栈

- React + TypeScript + Vite。
- React Router。
- TanStack Query。
- Vitest + React Testing Library。
- Playwright。

## 目录结构

```text
apps/web/src/
  app/
    router.tsx
    queryClient.ts
  api/
    client.ts
    types.ts
    incidents.ts
    approvals.ts
    runbooks.ts
  pages/
    IncidentsPage.tsx
    IncidentDetailPage.tsx
    AgentRunPage.tsx
    ApprovalsPage.tsx
    ReportPage.tsx
  components/
    layout/AppShell.tsx
    incident/IncidentTable.tsx
    incident/EvidencePanel.tsx
    agent/RunTimeline.tsx
    agent/ToolCallList.tsx
    approval/ApprovalDialog.tsx
    report/ReportView.tsx
    common/StatusBadge.tsx
    common/ErrorState.tsx
    common/EmptyState.tsx
  tests/
```

## 页面

### Incident 列表页

路径：`/incidents`。

功能：

- 按 status、service、severity 过滤。
- 展示 service、severity、status、alert_name、root_cause_summary、updated_at。
- 点击进入详情。
- 每 5 秒轮询未完成 incident。

### Incident 详情页

路径：`/incidents/:incidentId`。

功能：

- 展示报警原文。
- 展示 root cause。
- 展示 evidence 列表。
- 展示 recommended actions。
- 链接到 Agent run 和 report。

### Agent Run 轨迹页

路径：`/agent-runs/:agentRunId`。

功能：

- timeline 展示每个 LangGraph 节点。
- 展示节点状态、耗时、输入摘要、输出摘要。
- 展示工具调用列表和 cache hit。
- 展示 token 使用和 compression events。

### 审批页

必需路径：`/approvals`。Incident 详情页可以内嵌当前 incident 的审批摘要，但不能替代全局审批页。

功能：

- 通过 `GET /api/approvals?status=waiting` 拉取待审批 action。
- 展示 risk level、reason、rollback_plan。
- L2 approve/reject 提交 `approver` 和 `comment`。
- L3 approve 必须展示二次确认控件，并提交 `risk_ack=true`、`confirm_action_type`、`confirm_target`。
- reject 不要求二次确认，但必须提交拒绝原因。
- 提交后刷新 approval、incident 和 run 状态。

### 报告页

路径：`/incidents/:incidentId/report`。

功能：

- 展示 root cause、impact、timeline、actions、follow_ups。
- 支持重新生成报告。
- 显示引用 evidence id。

## API client

`api/client.ts`：

- 封装 base URL。
- 自动注入 `X-Request-Id`。
- 统一处理 `{ error }` 响应。
- 使用 TypeScript 类型与后端 OpenAPI 保持一致。

## 状态设计

- 加载态：表格和详情区域 skeleton。
- 空态：无 incident、无 evidence、无 report。
- 错误态：展示 error code、message、request_id。
- 轮询：只有 `queued`、`running`、`waiting_approval` 状态轮询。

## 测试

- 组件测试覆盖 table、timeline、approval dialog、report view。
- API mock 覆盖成功、失败、空列表、审批冲突。
- Playwright 覆盖完整链路：创建 alert、查看详情、审批、查看报告。
- 覆盖率 statements、branches、functions、lines 均 > 80%。
