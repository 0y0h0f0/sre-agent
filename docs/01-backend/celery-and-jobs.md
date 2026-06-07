# Celery 与异步任务

## Celery 配置

Celery app 位于 `apps/worker/celery_app.py`。

关键配置：

- broker：`CELERY_BROKER_URL`，默认 Redis db 1。
- result backend：`CELERY_RESULT_BACKEND`，默认 Redis db 2。
- `task_track_started=True`。
- `task_acks_late=True`。
- `task_reject_on_worker_lost=True`。
- `worker_prefetch_multiplier=1`。
- `task_time_limit=120`。
- `task_soft_time_limit=90`。
- serializer/result serializer 均为 JSON。
- `task_always_eager` 由 `CELERY_TASK_ALWAYS_EAGER` 控制。
- worker metrics server 默认端口 `9800`。

## 主要任务

| 任务 | 入口 | 说明 |
| --- | --- | --- |
| `run_incident_diagnosis` | `apps.worker.tasks.run_incident_diagnosis` | 运行 LangGraph 诊断 |
| `resume_incident_after_approval` | `apps.worker.tasks.resume_incident_after_approval` | 审批后恢复 LangGraph |
| `send_email_notification` | `apps.worker.tasks.send_email_notification` | 发送排队邮件 |
| `send_daily_incident_summary` | `apps.worker.tasks.send_daily_incident_summary` | 发送每日摘要 |
| `auto_approve_stale_approvals` | `apps.worker.tasks.auto_approve_stale_approvals` | 按配置自动审批低风险过期请求 |

## Beat schedule

`celery_app.conf.beat_schedule` 包含：

- `daily-incident-summary`：每天 09:00，发送每日事故摘要。
- `auto-approve-stale-approvals`：每 60 秒扫描一次待审批。

自动审批只允许 L0/L1/L2，配置上限超过 L2 时任务会跳过。L3+ 不允许自动审批。

## 诊断任务流程

1. API 创建 incident 和 agent run。
2. API 调用 `enqueue_diagnosis_task(incident_id, agent_run_id)`。
3. Worker 打开数据库 session。
4. 通过 repository 锁定 agent run row。
5. 如果 run 已是终态，返回 idempotent。
6. 如果 run 正在运行且未超过 orphan timeout，返回 idempotent。
7. 如果 run waiting approval，返回 idempotent。
8. 标记 run 为 running，incident 为 diagnosing。
9. 构造 `AgentDeps`。
10. 初始化 PostgreSQL checkpointer。
11. 调用 `AgentRunner.run()`。
12. 根据结果更新 run、incident、metrics、通知和报告。

## 幂等性

Celery 可能重复投递任务。诊断任务用数据库锁和 run 状态保证幂等：

- terminal statuses：不重新运行。
- running：未超时则不重新运行。
- waiting approval：不重新运行。
- orphaned running：超过 `TASK_ORPHAN_TIMEOUT_SECONDS` 后允许重试执行。

审批恢复任务同样锁定 agent run，只允许 `waiting_approval` 状态进入恢复。

## Checkpointer fail closed

真实 PostgreSQL 场景下，`_build_checkpointer()` 使用 `langgraph.checkpoint.postgres.PostgresSaver`。如果 checkpointer 初始化或 setup 失败，任务抛出 `DependencyUnavailableError`，拒绝在没有审批 gate 的情况下继续运行。

SQLite、memory 或测试场景可以返回 `None`，用于 deterministic test harness。

## 通知任务

诊断完成、审批请求、报告生成会排队邮件事件。邮件 service 先写 `email_log`，再由 `send_email_notification` 异步发送。

发送失败时：

- retryable 错误最多重试 3 次。
- `email_log.attempts` 和 `last_error` 更新。
- 常规测试默认不发送真实邮件。

## Worker 依赖构造

`_build_deps()` 构造：

- request-local tool cache。
- MetricsTool、LogsTool、TraceTool、GitChangeTool。
- K8sDiagnosticsTool、DbDiagnosticsTool。
- RunbookRetriever 和 RunbookSearchTool。
- MemoryStore 和 ContextBuilder。
- LLM adapter。
- node tracer。
- tool call recorder。

node tracer 会写 `agent_run_nodes`，并通过 Redis pub/sub 发布 WebSocket 事件。

tool call recorder 会写 `tool_calls`。
