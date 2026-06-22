# 报告生成、版本与事件生命周期技术深挖

**最后更新：** 2026-06-23

本文按当前代码说明 incident report 如何生成、如何再生成新版本，以及 worker 如何同步 incident / agent run 的生命周期状态。它补充 [告警到报告技术深挖](alert-to-report-deep-dive.md)、[Agent 工作流](../02-agent/workflow.md)、[API 参考](../01-backend/api-reference.md)、[数据模型](../01-backend/data-model.md) 和 [React 控制台](../06-frontend/react-console.md)。

阅读本文后应能回答：

- `generate_report` 节点如何把 root cause、evidence、actions、verify gates 写入报告。
- `POST /api/incidents/{incident_id}/report/regenerate` 为什么创建新版本而不是覆盖旧报告。
- `incident.root_cause_summary`、`agent_runs.state` 和 `incident_reports` 各自是什么 source。
- waiting approval、succeeded、failed 时 incident / run 状态如何同步。
- 报告邮件、前端报告页和调试入口分别看哪里。

## 代码入口

| 入口 | 职责 |
|------|------|
| `packages/agent/nodes/generate_report.py` | LangGraph 报告节点，生成并持久化 `IncidentReport` |
| `packages/agent/graph.py` | `verify -> generate_report -> persist_memory -> END` 路由 |
| `apps/worker/tasks.py` | run 领取、waiting/terminal 状态同步、报告通知 |
| `packages/db/repositories/reports.py` | report 创建、latest 查询、next version |
| `packages/db/models.py` | `IncidentReport` 模型和 `(incident_id, version)` 唯一约束 |
| `apps/api/services/report_service.py` | 最新报告读取、再生成报告、通知入队 |
| `apps/api/routers/reports.py` | `GET /report`、`POST /report/regenerate` |
| `apps/api/schemas/reports.py` | `IncidentReportResponse` |
| `apps/api/services/email_service.py` | `incident_report` 邮件内容生成 |
| `apps/web/src/App.tsx` | `ReportPage` / `ReportView` |
| `apps/web/src/api.ts` | `getIncidentReport()` / `regenerateIncidentReport()` |
| `tests/integration/test_report_api.py` | report regenerate 版本和 409 测试 |

## 一句话模型

报告链路有两条入口，但都只追加版本：

```text
Graph path:
verify/report route
  -> generate_report
  -> incident_reports vN
  -> persist_memory
  -> worker mark run succeeded and finalize incident
  -> report notification

API path:
POST /api/incidents/{incident_id}/report/regenerate
  -> latest AgentRun + current evidence/actions
  -> incident_reports vN+1
  -> report notification
```

`GET /api/incidents/{incident_id}/report` 总是返回当前 incident 的最新版本。旧版本保留在 `incident_reports` 表中，当前没有公开的“按版本读取”HTTP endpoint。

## 数据对象边界

| 对象/字段 | 存放位置 | 作用 | 不是 |
|-----------|----------|------|------|
| Incident status | `incidents.status` | 列表和详情页的生命周期状态 | 不代表 graph checkpoint |
| Root cause summary | `incidents.root_cause_summary` | incident 列表/详情的展示摘要 | 不替代报告全文或 evidence 引用 |
| Agent run state | `agent_runs.state` | 前端展示、再生成报告的输入之一 | 不替代 LangGraph checkpoint |
| Incident report | `incident_reports` | 版本化报告历史 | 不覆盖旧版本 |
| Evidence ids | `evidence_items.evidence_id` + response `evidence_ids` | 报告引用证据的可追溯 ID | 不等于 report body 中所有文字都已逐条验证 |
| Email log | `email_logs` | 报告通知发送记录和去重依据 | 不决定报告是否存在 |

核心规则：业务表保存展示和审计事实，checkpoint 保存 graph 恢复事实，报告表保存报告历史版本。

## Graph 报告生成

`generate_report` 节点读取当前 state 中的：

- `root_cause`
- `recommended_actions`
- 六类采集证据：metrics、logs、traces、deployment、k8s、db
- `verify_evidence`
- `runbook_context` 中已经持久化过的 runbook evidence
- `verify_result`
- `verify_gates`
- `needs_human_review`
- `errors`

报告 prompt 会把 evidence 摘要压成列表，每项包含：

- `evidence_id`
- `type`
- `source`
- `source_id`
- `source_path`
- `summary`

prompt 明确要求 evidence-backed claim 引用 evidence ID。真实 provider 或 FakeLLM 输出会先走 `extract_json()`；如果 LLM 调用或 JSON 解析失败，节点使用 deterministic fallback report。

LAT-07 后，`generate_report` 不再把完整 evidence payload、raw log samples 或完整 action list 直接序列化到 LLM prompt。节点先调用 `Compressor.compress_report_inputs()`，生成 compact report context：最多 12 条 evidence summary、按 type 统计、retained/omitted/all evidence IDs、runbook chunk IDs、最多 10 条 action trajectory 和最多 5 条结构化错误摘要。该压缩会追加 `scope="report_generation"` 的 compression event。

当 `LLM_DETERMINISTIC_REPORT_ENABLED=true` 时，节点跳过报告 LLM 调用，直接走 deterministic report builder。该模式仍创建新的 `incident_reports` 版本，仍合并 evidence IDs / runbook chunk IDs，不改变报告 schema 或版本追加语义。

`generate_report` 还会强制注入几个 deterministic 字段，不信任 LLM 自己决定：

| 字段 | 来源 | 说明 |
|------|------|------|
| `verify_result` | state | 最终验证结果 |
| `verify_gates` | state | 每个 gate 的 verdict、required、summary、evidence IDs |
| `needs_human_review` | state | evidence cross-validation 冲突标记 |
| manual review follow-up | deterministic append | `needs_human_review=true` 时追加人工复核事项 |
| `evidence_ids` / `runbook_chunk_ids` | deterministic merge | 从 compact report context、root cause、state 和 runbook evidence 合并，避免依赖模型完整输出追踪字段 |

创建报告时：

```text
repo.next_version(incident_id)
repo.create(..., version=version, body_markdown=json.dumps(report_data, indent=2))
```

节点返回的 state 会包含：

```text
incident_report = {
  "report_id": "...",
  "version": N,
  ...report_data
}
phase = "report_generated"
```

`generate_report` 节点自身不提交事务。事务提交由 worker 成功路径负责。

## Runbook Evidence

`retrieve_runbook` 会把 runbook context 命中持久化为 evidence，并把 `evidence_id` 回填到 `runbook_context`。`generate_report` 只把已经带 `evidence_id` 的 runbook chunk 纳入报告 evidence 列表。

这样做的效果是：

- 报告可以引用 runbook chunk 的 evidence ID。
- `source_id` 优先使用 chunk/source id。
- `source_path` 从 nested payload 或 chunk 字段中取。
- 未持久化、没有 evidence ID 的 runbook context 不会被当作可引用证据。

## Report Repository and Versioning

`IncidentReport` 模型字段：

| 字段 | 说明 |
|------|------|
| `report_id` | public ID，`rpt_` 前缀 |
| `incident_id` | FK 到 incident |
| `agent_run_id` | FK 到生成报告的 run |
| `version` | incident 内递增版本 |
| `root_cause` | 报告根因摘要 |
| `impact` | 影响描述 |
| `timeline` | JSON 列表 |
| `actions` | JSON 列表 |
| `follow_ups` | JSON 列表，元素可以是 dict 或 string |
| `body_markdown` | 当前 graph 路径为 JSON dump；API 再生成路径为 Markdown-like 文本 |
| `created_at` | 创建时间 |

数据库约束：

```text
UniqueConstraint("incident_id", "version", name="uq_report_incident_version")
```

`IncidentReportRepository.next_version()` 读取 latest report，返回 `latest.version + 1`；没有旧报告时返回 `1`。`get_latest_for_incident()` 按 `version desc, id desc` 取最新。

当前实现没有显式锁定 next version，因此并发再生成同一个 incident 的报告时，数据库唯一约束是最后防线。常规 UI/API 流程是单次点击触发，测试覆盖的是顺序再生成。

## API Report Regeneration

`ReportService.regenerate()` 的输入不是重新跑 LangGraph，而是读取当前持久化数据：

```text
require incident
latest AgentRun
list evidence
list actions
run.state["incident_report"]
next report version
create report
commit
enqueue incident_report notification
```

无 latest agent run 时返回 409：

```text
CONFLICT: incident has no agent run to build a report from
```

字段回退顺序：

| 字段 | 首选 | 回退 |
|------|------|------|
| root cause | `run.state.incident_report.root_cause` | `incident.root_cause_summary` -> `run.state.root_cause.summary` -> 默认文案 |
| impact | `run.state.incident_report.impact` | `"{severity} incident affecting {service}"` |
| timeline | `run.state.incident_report.timeline` | incident start + 前 8 条 evidence + actions |
| actions | `run.state.incident_report.actions` | 当前 `actions` 表摘要 |
| follow-ups | `run.state.incident_report.follow_ups` | 默认 review threshold/update runbook |

`body_markdown` 在再生成路径中由 `_body_markdown()` 组装，包含 Root cause、Impact、Timeline、Actions、Follow-ups。它不是重新调用 LLM，也不会覆盖旧报告。

## Latest Report API

`GET /api/incidents/{incident_id}/report`：

- incident 不存在：404 incident。
- report 不存在：404 report。
- report 存在：返回最新版本。

响应字段：

| 字段 | 来源 |
|------|------|
| `report_id`、`version`、`root_cause`、`impact`、`timeline`、`actions`、`follow_ups`、`body_markdown`、`created_at` | `incident_reports` |
| `incident_id`、`agent_run_id` | `incident_reports` |
| `evidence_ids` | 当前 incident 的所有 `evidence_items` |

注意：API response 的 `evidence_ids` 是当前 incident 证据列表，不是逐字解析 `body_markdown` 后得到的引用集合。Graph 生成路径会把 report data 中的 evidence IDs 写进 body/state；API schema 额外提供当前 incident 证据 ID 便于前端展示。

## Worker Lifecycle Sync

Worker 初始领取 run：

```text
SELECT ... FOR UPDATE AgentRun
queued/open -> running
incident.status = diagnosing
commit
```

重复投递处理：

- terminal run：直接 idempotent return。
- running 且未超过 orphan timeout：idempotent return。
- waiting approval：idempotent return。
- running 超过 orphan timeout：允许重新执行。

等待审批：

```text
_sync_incident_diagnosis(incident, state)
_handle_waiting_approval(run, state)
commit
notify diagnosis complete
notify approval requests
```

`_handle_waiting_approval()` 将 `agent_runs.status` 设为 `waiting_approval`，并保存 sanitized display state。当前代码不会在这里把 `incidents.status` 设置成 `waiting_approval`；incident 仍保留先前状态，通常是 `diagnosing`。审批列表和 Agent run status 是等待审批的主要 source。

成功终态：

```text
state_dict = _sanitize_state(result.state)
_sync_incident_diagnosis(incident, state_dict)
_populate_run_metrics(run, state_dict, tool_cache)
runs.mark_succeeded(run, state_dict)
incident.status = mitigated if execution_results else resolved
commit
notify diagnosis complete
notify report generated
```

失败：

- graph 返回 failed：`GRAPH_FAILED`，抛 `TransientError`，可触发 Celery autoretry。
- 未分类异常：rollback 后标记 `DIAGNOSIS_FAILED` 或 `RESUME_FAILED`，再抛出。

## Root Cause Summary Sync

`_sync_incident_diagnosis()` 按以下优先级同步 `incident.root_cause_summary`：

1. `state.root_cause.summary`
2. `state.incident_report.root_cause`
3. `state.diagnosis_rationale`

这使列表页和详情页能显示一个短摘要，但它不是 report source of truth。报告历史仍在 `incident_reports`，完整诊断 state 仍在 `agent_runs.state` 和 checkpoint 中。

## Report Notification

Graph 成功路径中，worker 调用 `_notify_report_generated(state_dict, db=db)`：

- 从 `state.incident_report.report_id` 取报告 ID。
- 通过 `EmailLogRepository.exists_for_event(notification_type="incident_report", related_report_id=report_id)` 去重。
- 没有重复时入队 `incident_report` email notification。

API 再生成路径在 commit 后直接调用注入的 notification enqueue：

```text
enqueue_notification("incident_report", {"report_id": report.report_id})
```

`EmailService._incident_report()` 会：

- 用 report ID 读取 `IncidentReport`。
- 读取 incident。
- 构造 `/incidents/{incident_id}/report` 和 incident URL。
- 渲染 `incident_report.html`。
- text body 包含 `report.body_markdown` 和 report URL。
- 写 `related_incident_id`、`related_agent_run_id`、`related_report_id`。

通知失败不应改变报告是否已创建。

## Frontend Report Page

前端报告页行为：

| 行为 | 代码 |
|------|------|
| 读取报告 | `useQuery(['incident-report', incidentId], getIncidentReport)` |
| 404 | 显示“无可用报告”空态 |
| 非 404 错误 | 标准 `ErrorState` |
| 生成/重新生成 | `regenerateIncidentReport(incidentId)` |
| 成功后刷新 | invalidate `['incident-report', incidentId]` |
| 展示字段 | version、agent run、evidence count、created_at、root cause、impact、timeline、actions、follow-ups、evidence IDs |

按钮文案取决于当前是否已有 `query.data`：

- 有报告：`重新生成`
- 无报告：`生成`

前端只显示 latest report，不提供历史版本选择，不决定版本号，也不覆盖旧版本。

## Lifecycle State Table

| 场景 | AgentRun status | Incident status | Report |
|------|-----------------|-----------------|--------|
| alert 刚入库 | `queued` | `open` | 无 |
| worker 已领取 | `running` | `diagnosing` | 无或旧版本 |
| L2/L3 等待审批 | `waiting_approval` | 通常仍为 `diagnosing` | 可能无报告 |
| run 成功且没有 execution results | `succeeded` | `resolved` | graph 路径通常已有新版本 |
| run 成功且有 execution results | `succeeded` | `mitigated` | graph 路径通常已有新版本 |
| graph/worker 失败 | `failed` | 当前实现不统一改为 `failed` | 可能无新版本 |
| API 再生成成功 | 不改变 run status | 不改变 incident status | 新增 latest version |

`failed` 是合法 incident status enum，但当前 worker 失败路径主要标记 run failed，并不在所有失败场景自动把 incident.status 改成 failed。

## 常见误区

| 误区 | 正确理解 |
|------|----------|
| 重新生成会覆盖旧报告 | 不会。每次 create 新 `version` |
| `GET /report` 能看历史版本 | 不能。它只返回 latest |
| `agent_runs.state` 是恢复 source | 不是。它是展示快照；恢复依赖 checkpoint |
| waiting approval 时 incident status 一定是 `waiting_approval` | 当前 worker 主要设置 run status；incident 通常仍是 `diagnosing` |
| `body_markdown` 总是 Markdown | graph 生成路径当前写 JSON dump；API 再生成路径写 Markdown-like 文本 |
| 报告通知失败会回滚报告 | 不会。通知是后置 best-effort/队列动作 |
| API 再生成会重新跑 Agent | 不会。它读取最新 run state、evidence 和 actions |

## Debug Checklist

报告不存在：

- 查 latest agent run 是否 `succeeded` / `waiting_approval` / `failed`。
- 查 `agent_run_nodes` 是否有 `generate_report`，状态是否 failed。
- 查 `incident_reports` 是否已有对应 `incident_id`。
- 对 API regenerate，查 incident 是否至少有一个 agent run；没有 run 会 409。

报告版本不递增：

- 查 `incident_reports` 的 `(incident_id, version)`。
- 查是否并发调用 regenerate 造成唯一约束冲突。
- 查 `IncidentReportRepository.get_latest_for_incident()` 是否能读到最新行。

根因摘要和报告不一致：

- 查 `incidents.root_cause_summary` 是否来自旧 run 或人工 root cause feedback。
- 查 latest `agent_runs.state.incident_report`。
- 查 latest `incident_reports.root_cause`。

报告没有 evidence ID：

- 查 `evidence_items` 是否已持久化。
- 查 state evidence 是否回填 `evidence_id`。
- 查 runbook context 是否有 `evidence_id`，没有时不会作为可引用 runbook evidence 纳入 graph report。

报告邮件没发：

- 查 `email_logs` 是否已有同 `related_report_id` 的 `incident_report` 事件。
- 查 notification enqueue 是否被调用。
- 查 `EmailService._incident_report()` 是否能通过 report ID 读取报告。

前端报告页空态：

- 查 API 是否返回 404 report。
- 点击“生成”后查 `POST /report/regenerate` 是否 201。
- 成功后查 query invalidation 是否重新请求 latest report。

## 相关测试入口

按变更范围选择测试；Codex 不直接运行测试套件，用户本地运行后回贴结果：

```bash
pytest tests/integration/test_report_api.py -v
pytest tests/unit/test_agent_nodes.py -k "report" -v
pytest tests/integration/test_graph_flow.py -k "report" -v
```

前端报告页变更：

```bash
cd apps/web
npm run test:coverage
```
