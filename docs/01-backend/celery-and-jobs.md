# Celery 与异步任务

**最后更新：** 2026-06-14

## Celery 应用

`apps/worker/celery_app.py` 定义单一 Celery 实例：`sre_incident_response_agent`。

| 配置 | 当前值/来源 | 说明 |
|------|-------------|------|
| broker | `CELERY_BROKER_URL` | 默认 Redis db 1 |
| result backend | `CELERY_RESULT_BACKEND` | 默认 Redis db 2 |
| `task_track_started` | true | 记录 started 状态 |
| `task_acks_late` | true | 至少一次投递，任务完成后确认 |
| `task_reject_on_worker_lost` | true | worker 丢失时拒绝任务，触发重投递 |
| `worker_prefetch_multiplier` | 1 | 每个 worker 一次预取一个任务 |
| `task_time_limit` | 600 秒 | 硬超时 |
| `task_soft_time_limit` | 300 秒 | 软超时；诊断任务将其转换为 retryable transient error |
| `task_default_retry_delay` | 5 秒 | 默认重试延迟 |
| serializer | JSON | task/result 均为 JSON |
| `task_always_eager` | `CELERY_TASK_ALWAYS_EAGER` | 测试时同步执行 |
| timezone | `NOTIFICATION_TIMEZONE` | Beat 和通知使用 |
| `broker_connection_retry_on_startup` | true | 启动时重试 broker 连接 |
| `broker_pool_limit` | 10 | broker 连接池上限 |
| `result_expires` | 86400 秒 | result backend TTL |

Worker 还会在 `PROMETHEUS_METRICS_ENABLED=true` 且非 eager 模式下启动 Prometheus metrics HTTP server，端口为 `CELERY_METRICS_PORT`。

## Task 清单

| Task | 定义位置 | 触发方式 | 用途 |
|------|----------|----------|------|
| `run_incident_diagnosis` | `apps/worker/tasks.py` | `enqueue_diagnosis_task()`、alert/manual diagnose | 运行 LangGraph 诊断图 |
| `resume_incident_after_approval` | `apps/worker/tasks.py` | `enqueue_resume_task()`、approval service | 审批后用 checkpoint 恢复图 |
| `send_email_notification` | `apps/worker/tasks.py` | `enqueue_email_notification_task()` | 发送排队邮件事件 |
| `send_daily_incident_summary` | `apps/worker/tasks.py` | Celery Beat | 每日 incident 摘要 |
| `run_discovery_rerun` | `apps/worker/tasks.py` | discovery API | 手动发现扫描 |
| `auto_discovery_rerun` | `apps/worker/tasks.py` | worker startup hook、Celery Beat | 周期性自动发现；仅 discovery enabled 且 K8s live 时实际执行 |
| `auto_approve_stale_approvals` | `apps/worker/tasks.py` | Celery Beat | 自动批准超时 L0-L2 审批；绝不触碰 L3+ |
| `poll_alertmanager` | `apps/worker/tasks.py` | Celery Beat | 轮询 Alertmanager，创建 incident 并做 resolved inference |
| `run_eval_suite_task` | `apps/worker/eval_tasks.py` | eval API | 运行 eval suite 并持久化结果 |

## Beat 调度

`celery_app.conf.beat_schedule` 当前包含：

| 名称 | 调度 | Task |
|------|------|------|
| `daily-incident-summary` | 每天 09:00，按 `NOTIFICATION_TIMEZONE` | `send_daily_incident_summary` |
| `auto-approve-stale-approvals` | 每 60 秒 | `auto_approve_stale_approvals` |
| `poll-alertmanager` | `ALERT_POLL_INTERVAL_SECONDS` | `poll_alertmanager` |
| `periodic-discovery` | 每 30 分钟 | `auto_discovery_rerun` |

另外，`celery_app.on_after_finalize` 会在 worker finalize 后尝试入队一次 `auto_discovery_rerun`，作为启动发现。失败只记录 warning。

Beat 应保持单实例，避免周期任务重复触发。横向扩展 worker 可以通过增加 worker 副本完成。

## 诊断任务流程

```text
run_incident_diagnosis(incident_id, agent_run_id)
  -> SessionLocal()
  -> IncidentRepository.get_by_public_id()
  -> AgentRunRepository.get_for_update()
  -> 终态 / running 非孤儿 / waiting_approval 幂等返回
  -> running 孤儿超过 TASK_ORPHAN_TIMEOUT_SECONDS 时允许重新执行
  -> mark_running + incident.status=diagnosing + commit
  -> _build_deps(): tools, RAG, memory, LLM, executor, effective config
  -> _build_checkpointer(): PostgresSaver 或 sqlite/memory 下 None
  -> AgentRunner.run()
  -> waiting_approval: 保存展示 state，run.status=waiting_approval，通知审批
  -> failed: mark_failed + TransientError
  -> succeeded: sanitize state、同步 root cause、填 token/cache、mark_succeeded
  -> incident.status=mitigated（有 execution_results）或 resolved
  -> 通知诊断完成 / 报告生成
```

诊断任务使用 `autoretry_for=(TransientError,)`、`retry_backoff=True`、`max_retries=2`。`SoftTimeLimitExceeded` 会被转换为 `TransientError`，让 Celery 重试。

## 幂等与孤儿恢复

Celery 至少一次投递意味着同一任务可能被执行多次。当前幂等策略：

- `SELECT ... FOR UPDATE` 锁定 `agent_runs` 行。
- 终态 `succeeded/failed/cancelled` 直接返回 `idempotent=true`。
- `running` 且未超过 `TASK_ORPHAN_TIMEOUT_SECONDS` 直接返回。
- `running` 且超过 orphan timeout 视作前 worker 被杀死，允许重新执行。
- `waiting_approval` 不重新执行，只能由 resume task 推进。
- resume task 同样锁 run 行；若不是 `waiting_approval`，直接幂等返回。

## Checkpoint 策略

真实 PostgreSQL 配置下，worker 使用：

```python
from langgraph.checkpoint.postgres import PostgresSaver
```

`AgentRunner` 的 LangGraph config 固定为：

```python
{
    "configurable": {
        "thread_id": agent_run_id,
        "checkpoint_ns": "",
    }
}
```

规则：

- `thread_id` 必须等于 `agent_run_id`。
- 审批 resume 必须使用同一个 config。
- `agent_runs.state` 只做展示快照，不能当 checkpoint。
- PostgreSQL checkpointer 初始化失败时抛 `DependencyUnavailableError`，故障关闭，避免无 checkpoint 时绕过审批 gate。
- SQLite / memory DB 下可跳过 checkpointer，主要用于本地开发或 CI。

## `_build_deps()`

`apps/worker/tasks.py` 的 `_build_deps()` 构建 Agent 运行依赖：

| 依赖 | 来源/说明 |
|------|-----------|
| Effective config | production 读取 latest published `EffectiveConfigVersion`；local/demo 使用 settings defaults |
| `RequestLocalToolCache` | 单次 run 内工具缓存和 hit/miss 统计 |
| `MetricsTool` / `LogsTool` | 使用 effective Prometheus/Loki URL；URL 为空时 `UnavailableTool` |
| `TraceTool` | 通过 `build_trace_backend(settings)`，默认 fixture；无 Jaeger effective URL 时 degraded |
| `GitChangeTool` | fixture/GitHub/Argo CD deployment backend |
| `K8sDiagnosticsTool` | fixture 或 live read-only diagnostics |
| `DbDiagnosticsTool` | fixture 或 live read-only PostgreSQL diagnostics |
| `RunbookRetriever` + `RunbookSearchTool` | runbook chunk repository、hybrid search 配置 |
| `MemoryStore` + `ContextBuilder` | memory 和 prompt/context 预算 |
| LLM | `build_llm(settings)`，fake/disabled/real adapter |
| Executor | `build_executor_backend(settings)`，默认 fixture，显式 live K8s |
| Node tracer | 写 `agent_run_nodes` 并发布 WebSocket 事件 |
| Tool call recorder | 写 `tool_calls` |

Worker 在生产环境中只读取 published effective config，不读取 proposal 或 detected-only backend。

## 审批恢复流程

```text
resume_incident_after_approval(agent_run_id, decision)
  -> decision must be approved/rejected
  -> lock run row
  -> if status != waiting_approval: idempotent return
  -> mark_running + commit
  -> rebuild deps and checkpointer
  -> AgentRunner.resume(agent_run_id, decision)
  -> may pause again if rejection causes new plan requiring approval
  -> otherwise mark_succeeded and finalize incident status
```

审批 service 会先提交 approval/action 状态，再入队 resume，确保 worker 能读到最新审批结果。

## 邮件任务

邮件不是诊断主链路的硬依赖：

- `enqueue_email_notification_task()` 先写 `EmailLog`，再入队 `send_email_notification`。
- 入队失败会把 email log 标记为 enqueue failed，并重新抛出。
- 诊断完成、审批请求、报告生成会尽力入队通知；通知失败不应让诊断失败。
- `send_email_notification` 和 `send_daily_incident_summary` 最多重试 3 次。

## Discovery 任务

手动 discovery：

- API 层创建 `DiscoveryRun` 并持有 Redis lock 后入队 `run_discovery_rerun`。
- Task 构建 `DiscoveryRunner`，运行 Prometheus/Loki/Jaeger/K8s/backend endpoint detection。
- 结果写入 `DiscoveryStore`；有 backend endpoint 或 metric mapping 时创建 `DiscoveryProposal(status=pending_review)`。
- 完成后写 audit log。

自动 discovery：

- `auto_discovery_rerun` 由 startup hook 和 Beat 触发。
- `DISCOVERY_ENABLED=false` 时跳过。
- `K8S_BACKEND != live` 时跳过。
- Redis lock `lock:discovery:auto` 防止并发。

Production discovery 不应自动发布配置；worker 只使用 published config。

## Alertmanager Poll

`poll_alertmanager`：

- 仅当 `ALERT_SOURCE` 为 `poll` 或 `both` 时运行。
- 需要有效 poll scope：receiver、namespace allowlist、service allowlist 或 extra matchers。
- 按 filter hash 使用 Redis lock，避免同一 scope 并发 poll。
- 读取 published config 计算 Alertmanager URL。
- 通过 `AlertmanagerClient` 拉取 alerts。
- 使用与 webhook 相同 fingerprint 规则创建 incident，保证 dedup。
- 使用 `AlertPollCursor` 和 missing rounds 做 conservative resolved inference。

## Eval 任务

`run_eval_suite_task` 从 `eval_runs` 表读取 run，标记 running，调用 `packages.evals.datasets.harness.run_suite(suite)`，成功后写 metrics，失败后将错误写入 metrics 并标记 failed。CI smoke eval 必须使用 FakeLLM。

## 测试模式

`CELERY_TASK_ALWAYS_EAGER=true` 会在调用进程同步执行 task，适合集成测试。需要覆盖：

- enqueue 失败导致 run/incident 标记 failed。
- duplicate delivery 幂等返回。
- waiting approval 不重新执行。
- orphan running 超时后可重试。
- resume task 只恢复 waiting approval run。
- checkpointer 初始化失败故障关闭。
- notification failure 不阻断诊断主链路。
- poll/discovery lock 行为。
