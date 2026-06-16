# 告警到报告技术深挖

**最后更新：** 2026-06-15

本文按当前代码路径解释一条告警如何从 HTTP 请求变成 incident report。它不是 API 字段全集；字段契约见 [API 参考](../01-backend/api-reference.md)，节点细节见 [Agent 工作流](../02-agent/workflow.md)。

## 阅读目标

读完本文后，开发者应能回答：

- `POST /api/alerts` 为什么只入队，不直接运行 LangGraph。
- incident、agent run、evidence、action、approval、report 分别在什么时候写入。
- Celery 至少一次投递下，为什么不会重复创建审批或重复执行已完成 run。
- L2/L3 审批如何从 LangGraph interrupt 恢复，并且为什么 resume 必须读取数据库中的逐条 approval 状态。
- 前端 Agent Run 页面上的节点、工具调用、缓存和 token 信息来自哪里。

## 代码入口一览

| 阶段 | 主要代码 | 主要持久化对象 |
|------|----------|----------------|
| 告警摄取 | `apps/api/routers/alerts.py`、`apps/api/services/alert_service.py` | `Incident`、`AgentRun` |
| 手动诊断 | `apps/api/routers/incidents.py`、`apps/api/services/incident_service.py` | 新 `AgentRun` |
| 任务执行 | `apps/worker/tasks.py` | run status、node trace、tool call、run state |
| 图执行 | `packages/agent/runner.py`、`packages/agent/graph.py` | LangGraph checkpoint |
| 证据采集 | `packages/agent/nodes/collect_all_evidence.py`、`packages/agent/nodes/_persist.py` | `EvidenceItem` |
| 审批暂停 | `packages/agent/nodes/human_approval.py` | `Action`、`Approval` |
| 审批提交 | `apps/api/services/approval_service.py` | approval/action 状态、audit log |
| 报告生成 | `packages/agent/nodes/generate_report.py`、`apps/api/services/report_service.py` | `IncidentReport` |
| 前端读取 | `apps/api/services/agent_run_service.py`、`apps/web/src/api.ts`、`apps/web/src/App.tsx` | 只读 API 响应 |

## 1. 告警进入 API

`apps/api/routers/alerts.py` 的 `create_alert()` 做两件事：

1. 通过 `RateLimiter` 对 `alerts` scope 做 10/min 限流，key 优先使用 API key ID，未认证时使用 client IP。
2. 调用 `AlertService.create_alert()`，把业务逻辑交给 service。

`AlertService.create_alert()` 的关键顺序是：

```text
FalsePositivePatternRepository.should_suppress()
-> IncidentRepository.get_open_by_fingerprint()
-> create Incident(inc_*) + AgentRun(run_*)
-> commit
-> enqueue_diagnosis(incident_id, agent_run_id)
-> set celery_task_id
-> commit
-> best-effort enqueue notification
```

重要点：

- fingerprint 命中未关闭 incident 时直接返回 deduplicated response，不创建新 run。
- 创建 incident/run 后先提交，再入队 Celery；这样 worker 使用另一条 DB connection 时能读到记录。
- 入队失败会把 run 标为 failed，并把 incident 标为 failed，避免后续同 fingerprint 告警一直去重到一个永远不会诊断的 open incident。
- notification 入队失败不会阻断告警摄取。

手动触发 `POST /api/incidents/{incident_id}/diagnose` 走 `IncidentService.trigger_diagnosis()`。它会拒绝非 `force` 的活跃 run 重复诊断，然后创建新 `AgentRun` 并入队。

## 2. Worker 领取任务

Celery task `run_incident_diagnosis()` 只打开 DB session 并委托给 `run_incident_diagnosis_logic()`。真正的幂等控制在 logic 中：

```text
IncidentRepository.get_by_public_id()
-> AgentRunRepository.get_for_update()
-> terminal run: return idempotent
-> running and not orphaned: return idempotent
-> waiting_approval: return idempotent
-> mark_running + incident.status=diagnosing + commit
```

这里的 `SELECT ... FOR UPDATE` 是关键：Celery 至少一次投递时，两个 worker 可能拿到同一个任务。行锁让“检查状态 + 改成 running”成为串行操作，输掉竞争的 worker 会在赢家提交后看到已推进状态并返回幂等结果。

如果 run 长时间停留在 `running` 且超过 `TASK_ORPHAN_TIMEOUT_SECONDS`，worker 认为前一个进程已死亡，允许重新执行。

## 3. Worker 构造运行依赖

`_build_deps()` 把运行时依赖集中注入到 `AgentDeps`，节点本身不直接创建 DB session 或外部 client。

关键构造规则：

- production 读取 latest published `EffectiveConfigVersion`，再合并 active override、env 和 safe default；local/demo 使用 settings default。
- metrics/logs/trace 等 backend URL 缺失时返回 `UnavailableTool`，而不是把 `None` 传给真实工具构造器。
- `RequestLocalToolCache` 只作用于单次 run。
- `node_tracer` 写 `agent_run_nodes`，并通过 Redis Pub/Sub best-effort 发布 WebSocket 节点事件。
- `tool_call_recorder` 写 `tool_calls`，保留 query、result、cache key、cache hit 和摘要。
- executor 通过 `build_executor_backend(settings)` 创建，默认仍是 fixture。

`_build_checkpointer()` 在真实 PostgreSQL 配置下使用 `langgraph.checkpoint.postgres.PostgresSaver`。如果 checkpointer 初始化失败，worker 故障关闭；这是为了避免没有 checkpoint 时绕过人工审批 gate。

## 4. LangGraph 启动和 checkpoint

`AgentRunner.run()` 使用固定 config：

```python
{"configurable": {"thread_id": agent_run_id, "checkpoint_ns": ""}}
```

`thread_id` 等于 `agent_run_id`，因此审批恢复可以用同一个 run ID 找回 checkpoint。`AgentRun.state` 只保存展示/debug 快照，不能替代 LangGraph checkpoint。

初始 state 包含 incident/run ID、原始 alert payload、各类 evidence 列表、approval/execution/report/token/cache 字段，以及 `_interrupts_enabled`。当 checkpointer 存在时 `_interrupts_enabled=true`，L2/L3 会通过 `interrupt()` 暂停。

## 5. 证据采集和持久化

当前图上的证据节点是 `collect_all_evidence`，它并行调用六个采集器：

```text
metrics, logs, traces, deployment, k8s, db
```

实现上分三步：

1. 在线程池中运行采集器。
2. 每个线程用捕获版 `node_tracer` / `tool_call_recorder`，避免共享 DB session 跨线程写库。
3. 主线程合并 partial state，replay trace/tool call，并通过 `persist_evidence_batch()` 批量写入 `evidence_items`。

`persist_evidence_batch()` 会给每条 state evidence 回填 `evidence_id`，下游诊断、报告和前端都依赖这些 ID。若批量持久化失败，会清掉已回填的 `evidence_id`，防止后续节点引用不存在的 DB 行。

## 6. 诊断、计划和护栏

主路径在 `packages/agent/graph.py` 中连接：

```text
build_context -> diagnose -> compress_context
-> rank_hypotheses -> plan_actions -> guardrail_check
```

边界规则：

- `build_context` 使用 `ContextBuilder` 做 token budget 和上下文组装，不直接调用 LLM。
- `diagnose` 可调用 FakeLLM、disabled adapter 或真实 adapter；CI/smoke 必须使用 FakeLLM。
- `compress_context` 处理超预算上下文，避免大日志直接进入 prompt。
- `plan_actions` 只给建议动作。
- `guardrail_check` 才是最终风险分类入口。L2/L3 需要审批，L4 直接走报告，不进入审批。

如果 `diagnosis_rationale.missing_evidence` 非空且未超过 `MAX_DIAGNOSE_CYCLES`，图会进入 `collect_gap` 做一次受限补采，然后回到 `build_context`。

## 7. 审批暂停和恢复

`human_approval` 只处理 `requires_approval=true` 的动作。第一次进入时，它会为每个动作创建：

- `actions` 行，状态为 `waiting_approval`。
- `approvals` 行，状态为 `waiting`。

随后节点记录 `waiting_approval` trace，并在有 checkpointer 时调用 LangGraph `interrupt()`。worker 捕获 `GraphInterrupt` 后：

```text
sync incident root cause
-> run.status=waiting_approval
-> run.state=sanitized checkpoint state
-> commit
-> notify diagnosis complete / approval requests
```

审批 API 的关键顺序在 `ApprovalService`：

```text
lock approval
-> validate waiting status
-> for L3 validate risk_ack + confirm_action_type + confirm_target
-> update approval/action
-> write audit log
-> commit
-> only when no approval in this run is still waiting: enqueue resume
```

resume 不会盲信传入的单个 `decision`。`human_approval._apply_db_decisions()` 会重新读取每条 approval 的 DB 状态：

- approved：清除 `requires_approval`，允许进入执行。
- rejected：把 action 标为不可执行。
- waiting：保持等待，图继续停在 approval。

这能防止“一个 approved decision 放行整个批次”的错误，也避免 sibling approvals 被批准但永远不执行。

## 8. 执行、验证和报告

审批通过或 L0/L1 自动路径会进入：

```text
take_snapshot -> execute_action -> verify
```

`take_snapshot` 保存执行前证据/K8s 状态。`execute_action` 使用注入的 executor backend，默认 fixture；live backend 只能在显式启用后执行已允许的 Kubernetes restart/scale/rollback。`verify` 按 action capability metadata 执行只读 gates，最多 `MAX_VERIFY_CYCLES=2` 次验证/重规划。

报告由 `generate_report` 节点创建：

- 收集 metrics/logs/traces/deployment/k8s/db/verify evidence。
- 要求 evidence-backed claim 引用 evidence ID。
- LLM 报告失败时使用 deterministic fallback。
- 调用 `IncidentReportRepository.next_version()` 和 `create()` 写入 `incident_reports`。

`incident_reports` 对 `(incident_id, version)` 有唯一约束。`POST /api/incidents/{incident_id}/report/regenerate` 不覆盖旧报告，而是基于最新 run state、evidence 和 actions 生成新版本。

## 9. Run 结束后的状态同步

worker 成功返回前会：

- `_sanitize_state()` 移除以下划线开头的内部字段，并把 datetime 转成 ISO 字符串。
- `_sync_incident_diagnosis()` 把 root cause summary 同步到 incident。
- `_populate_run_metrics()` 写 prompt/completion token、provider cache hit/miss、app cache hit/miss。
- `AgentRunRepository.mark_succeeded()` 写最终 state、finished_at、duration。
- 若有 `execution_results`，incident 标为 `mitigated`；否则标为 `resolved`。
- best-effort 发送诊断完成和报告生成通知。

失败时，worker 会把 run 标为 failed，并记录 error code/message。`TransientError` 可触发 Celery autoretry。

## 10. 前端如何看到这条链路

前端 API client 在 `apps/web/src/api.ts` 中封装：

- `getIncident()` 读取 incident detail、evidence 和 actions。
- `listIncidentRuns()` 读取某个 incident 的 run 列表。
- `getAgentRun()` 读取 run state、node trace 和 tool calls。
- `listIncidentApprovals()` / `listApprovals()` 展示审批。
- `getIncidentReport()` / `regenerateIncidentReport()` 展示和重建报告。

Agent Run 页面还会调用 WebSocket ticket API，连接 `/api/ws/incidents/{incidentId}`。worker 的 `node_tracer` 写 `agent_run_nodes` 后会 best-effort 发布节点事件；前端用这些事件补充轮询数据，让运行中节点更快显示出来。

## 调试检查表

| 现象 | 优先检查 |
|------|----------|
| `POST /api/alerts` 返回 deduplicated | `incidents.fingerprint` 是否已有未关闭 incident |
| run 一直 `queued` | `agent_runs.celery_task_id`、Celery worker 是否运行、Redis broker 是否可达 |
| run 一直 `running` | `agent_run_nodes` 最后一个节点、worker 日志、orphan timeout |
| run 进入 `waiting_approval` | `actions` / `approvals` 是否有 waiting 行，前端 `/approvals` 是否能查到 |
| 审批后没有恢复 | 同 run 是否仍有 waiting approval；resume task 是否入队 |
| 报告不存在 | `generate_report` 节点 trace、`incident_reports` 是否有 v1、run 是否 failed/waiting |
| Agent Run 页面没有工具调用 | `tool_calls` 是否有记录；collect_all_evidence 是否在 trace replay 前失败 |
| token/cache 显示未知 | `llm_calls` 是否写入 state，`_populate_run_metrics()` 是否在成功路径执行 |

## 不要破坏的设计点

- API 线程只持久化和入队，不运行 LangGraph。
- `agent_runs.state` 是展示快照，不是 checkpoint source of truth。
- 审批恢复必须读取 DB 中每条 approval 的真实状态。
- L3 二次确认只能通过完整字段通过，email token 不允许批准 L3。
- L4 不进入审批或执行。
- 真实写路径只能通过已记录的 executor backend 和 guardrail/approval 链路。
