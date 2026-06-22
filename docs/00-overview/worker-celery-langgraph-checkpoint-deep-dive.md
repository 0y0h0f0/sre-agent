# Worker、Celery 与 LangGraph Checkpoint 技术深挖

**最后更新：** 2026-06-23

本文沿当前代码路径说明 API 入队后，Celery worker 如何领取诊断任务、保证幂等、构造 `AgentDeps`、初始化 LangGraph checkpointer、处理中断/恢复、记录 node/tool audit、同步 incident/run 状态、发送通知、执行 discovery/poll/eval 任务。它补充 [Celery 与异步任务](../01-backend/celery-and-jobs.md) 和 [Agent 工作流](../02-agent/workflow.md)：前者列出任务和调度，后者列出图节点；本文解释执行面如何把它们安全组合。

## 阅读目标

读完本文应能回答：

- Celery 的至少一次投递如何通过 run row lock 和状态检查变成幂等执行。
- 为什么 worker 要先把 run 标记为 `running` 并 commit，再运行 LangGraph。
- PostgreSQL `PostgresSaver` 如何初始化，为什么真实 DB 下 checkpointer 失败必须故障关闭。
- `AgentRunner.run()` 和 `AgentRunner.resume()` 使用的 LangGraph config 是什么。
- `human_approval` 如何避免重复创建审批、如何回读 DB 决策、何时再次 interrupt。
- Worker 如何写 `agent_run_nodes`、`tool_calls`、`agent_runs.state`、token/cache counters。
- Discovery、Alertmanager poll、邮件和 stale approval task 的执行边界是什么。
- 哪些失败会重试，哪些失败只降级或记录 warning。

## 代码入口

| 主题 | 当前入口 |
|------|----------|
| Celery app 和 Beat | `apps/worker/celery_app.py` |
| 诊断/resume/discovery/poll/email task | `apps/worker/tasks.py` |
| Eval task | `apps/worker/eval_tasks.py` |
| Agent runner | `packages/agent/runner.py` |
| LangGraph 构图 | `packages/agent/graph.py` |
| Human approval node | `packages/agent/nodes/human_approval.py` |
| Run repository | `packages/db/repositories/agent_runs.py` |
| Tool call repository | `packages/db/repositories/tool_calls.py` |
| Email log repository | `packages/db/repositories/email_logs.py` |
| Discovery store | `packages/discovery/store.py` |
| Alertmanager poll cursor | `packages/db/repositories/poll_cursor.py` |
| Worker integration tests | `tests/integration/test_worker_task.py`、`tests/integration/test_worker_tool_audit.py` |

## 总链路

```text
API service
  -> create Incident + AgentRun
  -> commit
  -> enqueue_diagnosis_task(incident_id, agent_run_id)
  -> save celery_task_id

Celery worker
  -> run_incident_diagnosis
  -> get AgentRun FOR UPDATE
  -> idempotency/orphan/waiting checks
  -> mark running + commit
  -> _build_deps()
  -> _build_checkpointer()
  -> AgentRunner.run()
       -> LangGraph StateGraph
       -> node_tracer / tool_call_recorder
       -> GraphInterrupt when approval required
  -> sync run/incident/report/notifications
```

审批恢复是同一条执行面的延续：

```text
ApprovalService
  -> update approval/action/audit
  -> commit
  -> if no waiting approvals for run: enqueue_resume_task(agent_run_id, decision)

Celery worker
  -> resume_incident_after_approval
  -> lock waiting run
  -> mark running + commit
  -> rebuild deps/checkpointer
  -> AgentRunner.resume(agent_run_id, decision)
```

## 1. Celery App Contract

`apps/worker/celery_app.py` 定义唯一 Celery app：`sre_incident_response_agent`。

关键配置：

| 配置 | 当前值/来源 | 影响 |
|------|-------------|------|
| Broker | `CELERY_BROKER_URL`，默认 Redis db 1 | task 投递。 |
| Result backend | `CELERY_RESULT_BACKEND`，默认 Redis db 2 | task result TTL。 |
| `task_acks_late=true` | 配置固定 | 任务完成后 ack，worker 崩溃可重投递。 |
| `task_reject_on_worker_lost=true` | 配置固定 | worker 丢失时拒绝任务。 |
| `worker_prefetch_multiplier=1` | 配置固定 | 每个 worker 一次预取一个任务。 |
| `task_soft_time_limit=300` | 配置固定 | 诊断任务把软超时转换为 retryable transient error。 |
| `task_time_limit=600` | 配置固定 | 硬超时。 |
| `task_always_eager` | `CELERY_TASK_ALWAYS_EAGER` | 测试可同步执行。 |
| Beat schedule | `celery_app.conf.beat_schedule` | 每日摘要、stale approval、Alertmanager poll、periodic discovery。 |

Worker 进程在 `PROMETHEUS_METRICS_ENABLED=true`、非 eager、且进程参数包含 `worker` 时启动 Prometheus metrics HTTP server，端口为 `CELERY_METRICS_PORT`。Celery inspect 等短生命周期命令只导入模块，不应抢占 metrics 端口。

## 2. Task Catalog

| Task | 入口 | 触发者 | 失败/重试 |
|------|------|--------|-----------|
| `run_incident_diagnosis` | `apps/worker/tasks.py` | alert/manual diagnose | `TransientError` autoretry，backoff，最多 2 次。 |
| `resume_incident_after_approval` | `apps/worker/tasks.py` | approval service | `TransientError` autoretry，backoff，最多 2 次。 |
| `send_email_notification` | `apps/worker/tasks.py` | notification enqueue | retryable result 最多 3 次。 |
| `send_daily_incident_summary` | `apps/worker/tasks.py` | Beat | retryable result 最多 3 次。 |
| `run_discovery_rerun` | `apps/worker/tasks.py` | discovery API | `TransientError` autoretry，最多 1 次。 |
| `auto_discovery_rerun` | `apps/worker/tasks.py` | startup hook / Beat | 不重试；返回 skipped/failed。 |
| `auto_approve_stale_approvals` | `apps/worker/tasks.py` | Beat | 不自动碰 L3+。 |
| `poll_alertmanager` | `apps/worker/tasks.py` | Beat | `TransientError` autoretry，最多 1 次。 |
| `run_eval_suite_task` | `apps/worker/eval_tasks.py` | eval API | 写 eval run 状态和 metrics。 |

Beat 应保持单实例。横向扩展普通 worker 可以增加 worker 副本，但周期任务重复会导致重复 poll/discovery/通知风险。

## 3. Diagnosis Task Idempotency

`run_incident_diagnosis_logic()` 是诊断主入口。它先读取 incident，再通过 `AgentRunRepository.get_for_update()` 锁定 run row。

状态判断：

| 当前 run status | 行为 |
|-----------------|------|
| `succeeded` / `failed` / `cancelled` | rollback 并返回 `idempotent=true`。 |
| `running` 且未超 orphan timeout | rollback 并返回 `idempotent=true`。 |
| `running` 且超过 `TASK_ORPHAN_TIMEOUT_SECONDS` | 视为前 worker 已死，允许重新执行。 |
| `waiting_approval` | rollback 并返回 `idempotent=true`，只能由 resume task 推进。 |
| `queued` 或可领取状态 | `mark_running()`，incident 标记 `diagnosing`，commit。 |

领取后立即 commit 的原因：

- 释放 `SELECT ... FOR UPDATE` 锁。
- 让重复投递的第二个 worker 看到 `running` 并短路。
- 保证后续 Graph 执行不是在长事务中持有锁。

这套策略配合 Celery `acks_late` 实现至少一次投递下的安全幂等。

## 4. Checkpointer Fail-Closed

`_build_checkpointer(settings)` 的规则：

```text
if database_url missing / sqlite / memory:
  -> return None
else:
  -> PostgresSaver.from_conn_string(...)
  -> saver.setup()
  -> return saver
```

真实数据库路径使用：

```python
from langgraph.checkpoint.postgres import PostgresSaver
```

SQLAlchemy URL 会先通过 `_postgres_saver_conn_string()` 去掉 driver suffix，例如：

```text
postgresql+psycopg://... -> postgresql://...
postgres+psycopg://...   -> postgres://...
```

真实 DB 下如果 `PostgresSaver` 初始化或 `setup()` 失败，worker 抛 `DependencyUnavailableError`，不退回 `None`。这是安全边界：没有 checkpointer 时 LangGraph 不能可靠 interrupt/resume；如果静默降级，会把需要审批的 L2/L3 路径变成无持久 checkpoint 的自动推进风险。

SQLite/memory 返回 `None` 是有意的 dev/test 路径。该路径只用于本地或测试，不能作为生产兜底。

## 5. AgentRunner and LangGraph Config

`packages/agent/runner.py` 固定使用：

```python
config = {
    "configurable": {
        "thread_id": agent_run_id,
        "checkpoint_ns": "",
    }
}
```

`AgentRunner.run()`：

- 调用 `build_graph(deps, checkpointer)`。
- 构造初始 `IncidentState`。
- `_interrupts_enabled = self.checkpointer is not None`。
- `graph.invoke(initial_state, config)`。
- 如果返回 state 包含 `__interrupt__` 或捕获 `GraphInterrupt`，返回 `waiting_approval`。
- 捕获 `GraphInterrupt` 后调用 `graph.get_state(config)` 取最新 checkpoint state，避免只返回空 initial state。

`AgentRunner.resume()`：

- 用同一个 config 重新编译图。
- 调用：

```python
graph.invoke(
    Command(
        resume={"decision": decision},
        update={"approval_decision": decision},
    ),
    config,
)
```

- 如果再次 interrupt，返回 `waiting_approval` 并从 checkpoint 取最新 state。

业务表中的 `checkpoint_thread_id`、`checkpoint_ns`、`latest_checkpoint_id` 是指针/展示字段；`agent_runs.state` 是展示快照，不是 checkpoint source of truth。

## 6. AgentDeps Construction

`_build_deps(db, settings, agent_run_id, incident_id)` 每次 run/resume 都重建依赖。

| 依赖 | 当前行为 |
|------|----------|
| EffectiveConfig | production 读取 latest published config + active overrides；local/demo 使用 settings defaults。 |
| Tool cache | `RequestLocalToolCache`，保存单次 run 工具 cache hit/miss。 |
| Metrics/logs | URL 来自 EffectiveConfig；缺 URL 用 `UnavailableTool`。 |
| Trace | 支持 fixture、Jaeger、Tempo、disabled；production fixture trace 返回 unavailable。 |
| Deployment | `GitChangeTool` 使用 fixture/GitHub/Argo CD backend。 |
| K8s diagnostics | fixture 或 live read-only diagnostics。 |
| DB diagnostics | fixture 或 live read-only PostgreSQL diagnostics。 |
| Runbook | `RunbookRetriever` + `RunbookSearchTool`。 |
| Memory/context | `MemoryStore` + `ContextBuilder`。 |
| LLM | `build_llm(settings)`，Fake/disabled/real adapter。 |
| Executor | `build_executor_backend(settings)`，默认 fixture，live K8s 显式 opt-in。 |
| Node tracer | 写 `agent_run_nodes` 并发布 WebSocket `node_update`。 |
| Tool recorder | 写 `tool_calls`。 |

Production worker 只读取 published EffectiveConfig 和 active overrides，不读取 discovery proposal 或 detected-only backend。缺失 backend 应降级为 `UnavailableTool`，不让构造函数因 `None` URL 崩溃。

## 7. Node and Tool Audit

`node_tracer()` 写 `AgentRunNode`：

- `node_id` 默认 `nd_`。
- `agent_run_id` 默认当前 run。
- `name`、`status`、`started_at`、`finished_at`。
- `duration_ms`。
- `input_summary` / `output_summary` 截断到 500 字符。
- `error_message` 截断到 500 字符。

写 DB 后，它 best-effort 调用 `publish_node_event()`，向 Redis channel `incident:{incident_id}` 发布 WebSocket 事件。发布失败只记录 warning，不影响诊断。

`tool_call_recorder()` 通过 `ToolCallRepository.create()` 写：

- tool name / node name。
- Pydantic query。
- `ToolResult`。
- input/output summary。
- duration、cache key、cache hit、error。

这些记录供 Agent Run API、前端时间线/工具列表、工程指标和排障使用。

## 8. Waiting Approval Path

`human_approval` node 的安全点：

- 首次进入时，为需要审批的 action 创建 `Action` 和 `Approval`。
- 写 node trace，state 中保存 `approval_status={"status":"waiting","approval_ids":[...]}`。
- 如果 `_interrupts_enabled=true`，调用 `interrupt()`，让 LangGraph 抛 `GraphInterrupt` 并保存 checkpoint。
- 如果 checkpoint state 稀疏，resume 时可通过 DB 中 existing approvals 恢复当前 approval batch，避免重复创建审批。
- Resume path 不盲目把一个批次决策套给所有 action；它逐个读取 DB approval 状态。

DB 决策映射：

| Approval status | 对 action 的影响 |
|-----------------|------------------|
| `approved` | 清除 `requires_approval`，允许执行。 |
| `rejected` | `allowed=false`，清除 `requires_approval`，进入重规划/报告路径。 |
| `waiting` | 保持等待；`execute_action` 会跳过，不会自动执行。 |

无 checkpointer的 dev/test 路径会 auto-approve 非 L3 approval，方便单元测试推进；L3 永远不会被该路径自动批准。

## 9. Resume Task

`_resume_incident_logic()`：

```text
validate decision in {"approved", "rejected"}
-> lock AgentRun FOR UPDATE
-> if status != waiting_approval: idempotent return
-> mark running + commit
-> rebuild deps
-> build checkpointer
-> AgentRunner.resume(agent_run_id, decision)
-> waiting_approval: sync state, run stays waiting, send approval notifications
-> failed: mark_failed + TransientError
-> succeeded: sanitize state, mark_succeeded, finalize incident
```

Approval service 先 commit approval/action/audit，再入队 resume。Worker resume 前再次锁 run row，避免重复 resume 并发推进。

Resume 后可能再次 `waiting_approval`。例如拒绝后重规划提出新 L2/L3 action，图会创建新 approval batch 并再次暂停。

## 10. Run State Synchronization

成功终态：

- `_sanitize_state()` 去掉以 `_` 开头的内部字段，并把 datetime 转成 ISO 字符串。
- `_sync_incident_diagnosis()` 从 `root_cause.summary`、`incident_report.root_cause` 或 `diagnosis_rationale` 同步 `incident.root_cause_summary`。
- `_populate_run_metrics()` 写 token/cache counters。
- `runs.mark_succeeded(run, state_dict)` 写 `agent_runs.state`、`finished_at`、`duration_ms`。
- 如果 state 有 `execution_results`，incident 标记 `mitigated`；否则标记 `resolved`。

等待审批：

- `_handle_waiting_approval()` 把 run status 设为 `waiting_approval`。
- 保存 sanitized display state。
- 发送诊断完成和 approval request 通知。

失败：

- Graph failed 时标记 `GRAPH_FAILED` 并抛 `TransientError`，触发 Celery autoretry。
- 未分类异常会 rollback，标记 `DIAGNOSIS_FAILED` 或 `RESUME_FAILED`，再抛出。

报告版本、`generate_report`、report regeneration、report notification 和 incident/run lifecycle 的完整字段边界见 [报告生成、版本与事件生命周期技术深挖](report-generation-incident-lifecycle-deep-dive.md)。

注意：`agent_runs.state` 用于展示和报告辅助，不用于恢复图。

## 11. Token and Cache Metrics

`_populate_run_metrics()` 当前读取两类指标：

| 指标 | 来源 | 写入字段 |
|------|------|----------|
| LLM token usage | `state["llm_calls"][].usage` | `total_prompt_tokens`、`total_completion_tokens` |
| LLM cached token / duration summary | `state["llm_calls"][].usage.cached_prompt_tokens`、`duration_ms` | `agent_runs.state.llm_metrics_summary`、`state.token_usage` |
| Provider cache | `state["llm_calls"][].provider_cache_status` (`hit`/`miss`/`unknown`)，legacy `cache_hit` 仅作兼容 fallback | DB: `provider_cache_hit_count`、`provider_cache_miss_count`; state summary: `provider_cache.unknown` |
| App tool cache | `RequestLocalToolCache` | `app_cache_hit_count`、`app_cache_miss_count` |

Provider prompt cache、tool request-local cache、app prompt segment cache 是不同概念，不能混用。
每次 provider 调用的 token/duration/cache runtime Prometheus 指标由 adapter 调用路径记录；worker 只从 `state["llm_calls"]` 汇总 run 级字段，不重复递增 per-call token counter。provider cache `unknown`、cached prompt token 总量和 LLM duration 摘要保存在 `agent_runs.state.llm_metrics_summary` / `state.token_usage`，不需要 DB migration。

## 12. Notifications

通知是 best-effort 增强，不是诊断主链路硬依赖。

| Helper | 去重条件 |
|--------|----------|
| `_notify_diagnosis_complete()` | existing email log by `related_agent_run_id`。 |
| `_notify_approval_requests()` | existing email log by `related_approval_id`。 |
| `_notify_report_generated()` | existing email log by `related_report_id`。 |

`enqueue_email_notification_task()` 先写 `EmailLog`，再入队 `send_email_notification`。如果 `.delay()` 失败，会把 email log 标记为 enqueue failed 并重新抛出；诊断 helper 捕获并记录 error，不让通知失败反向中断诊断。

## 13. Stale Approval Auto-Approve

`auto_approve_stale_approvals` 由 Beat 每 60 秒触发。

边界：

- `APPROVAL_AUTO_APPROVE_MINUTES <= 0` 时 disabled。
- `APPROVAL_AUTO_APPROVE_MAX_RISK` 超过 `L2` 时跳过。
- 只处理 waiting approvals。
- 只 auto-approve L0/L1/L2。
- 写 approval/action 状态和 audit log。
- 对所有已无 waiting approval 的 run 入队 resume。

L3+ 永远不会被 stale auto-approve 触碰。

## 14. Discovery Tasks

手动 discovery：

```text
Discovery API
  -> create DiscoveryRun
  -> acquire RedisLock("discovery:runner", ttl=300)
  -> enqueue_discovery_rerun_task()

run_discovery_rerun
  -> DiscoveryStore.get_run()
  -> _build_discovery_runner(settings)
  -> runner.run(run_id)
  -> store.finish_run()
  -> if backend endpoints or metric mappings: create proposal pending_review
  -> create discovery audit
  -> commit
```

失败时，task 尝试把 discovery run 标记 failed。Discovery proposal 不会自动变成 worker runtime config。

自动 discovery：

- startup hook 和 Beat 触发。
- `DISCOVERY_ENABLED=false` 跳过。
- `K8S_BACKEND != live` 跳过。
- Redis lock `lock:discovery:auto` 防止并发。
- 写 discovery run 和 audit。

Production discovery 不自动 publish；worker 仍只读取 published EffectiveConfig。

## 15. Alertmanager Poll Task

`poll_alertmanager` 的边界：

- 仅 `ALERT_SOURCE in ("poll", "both")` 时运行。
- 必须有有效 poll scope：receiver、namespace allowlist、service allowlist 或 extra matchers。
- 用 `_build_filter_hash(filters)` 生成稳定 filter hash。
- 用 Redis lock `lock:poll:alertmanager:{filter_hash}` 防止同一 scope 并发。
- 读取 latest published config 和 active overrides 得到 Alertmanager URL。
- 使用 `AlertmanagerClient` 拉取 alerts。
- 单轮最多处理 `ALERT_POLL_MAX_ALERTS_PER_ROUND` 和 `ALERT_POLL_MAX_NEW_INCIDENTS_PER_ROUND`。
- 与 webhook 路径使用同一 fingerprint 规则，保证 dedup。
- 使用 `AlertPollCursor` 记录 active/missing，并通过 conservative resolved inference 标记 incident resolved。
- poll audit 当前使用 `aud_` 兼容 ID。

单个 alert 创建失败只记录 warning 并继续，不阻断整个 poll round。

## 16. Eval Task

`run_eval_suite_task` 位于 `apps/worker/eval_tasks.py`：

- 从 `eval_runs` 表读取 eval run。
- 标记 running。
- 调用 `packages.evals.datasets.harness.run_suite(suite)`。
- 成功后写 metrics 并标记 completed。
- 失败后写错误 metrics 并标记 failed。

CI smoke eval 必须使用 FakeLLM，真实 LLM eval 只用于手动 full eval。

## 17. Safety Boundaries

Worker 执行面必须保持：

- API 请求线程不运行 LangGraph。
- 真实 PostgreSQL 下 checkpointer 初始化失败必须 fail closed。
- `agent_runs.state` 不是 checkpoint source of truth。
- `waiting_approval` run 不由诊断 task 重跑，只由 resume task 推进。
- Resume 后逐个读取 DB approval 状态，不执行仍 waiting 的 action。
- 无 checkpointer dev/test 自动批准不包括 L3。
- 默认 executor 是 fixture；live executor 只能通过 `EXECUTOR_BACKEND=live` 显式启用。
- Discovery result/proposal 不自动 publish production config。
- Poll/discovery/email/WebSocket 发布失败不得绕过 guardrail 或审批。

## 18. Test Coverage Map

相关测试入口：

| 主题 | 测试文件 |
|------|----------|
| Checkpointer sqlite/real DB fail-closed、worker 幂等、in-flight skip、通知去重 | `tests/integration/test_worker_task.py` |
| Node/tool audit | `tests/integration/test_worker_tool_audit.py` |
| 事务可见性 | `tests/integration/test_transaction_visibility.py` |
| EffectiveConfig 进入 worker deps | `tests/integration/test_worker_with_effective_config.py` |
| Agent graph approval/replan/checkpoint smoke | `tests/integration/test_graph_flow.py` |
| Human approval no-checkpointer/L3 防护 | `tests/unit/test_agent_nodes.py` |
| Alertmanager poll parser/filter/cursor/resolved inference | `tests/integration/test_poll_integration.py` |
| Discovery runner/store/models | `tests/unit/test_discovery_runner.py`、`tests/unit/test_discovery_store.py`、`tests/unit/test_discovery_models.py` |

按项目策略，Codex 不直接运行 pytest。需要本地验证时由用户执行：

```bash
pytest tests/unit tests/integration --cov=apps --cov=packages --cov-report=term-missing --cov-report=xml --cov-fail-under=80
```

## 19. Debug Checklist

| 现象 | 先看 | 常见原因 |
|------|------|----------|
| run 长期 queued | Celery worker、Redis broker、`celery_task_id` | worker 未启动、broker 不通、enqueue 失败。 |
| run running 但无节点 | `agent_run_nodes`、worker logs、checkpointer init | worker 正在构造 deps/checkpointer，或启动后立刻失败。 |
| 重复 task 没有重复执行 | task result、run status | 幂等短路是预期行为。 |
| waiting approval 后不恢复 | approvals 状态、resume task、worker logs | 仍有 waiting approval，或 resume task 未入队/未执行。 |
| resume 后再次 waiting | 新 approval batch | 拒绝后重规划提出了新 L2/L3 动作。 |
| checkpointer 报错 | DB URL、PostgresSaver setup、migrations | 真实 DB 下 fail closed，不能退回 no-checkpointer。 |
| Agent Run 页面无实时日志 | Redis Pub/Sub、WebSocket ticket、`agent_run_nodes` | WebSocket 是增强；先确认 DB node trace。 |
| tool_calls 为空 | `_build_deps()`、tool recorder、工具是否执行 | 工具构造 unavailable 或节点提前失败。 |
| Discovery 总是 skipped | `DISCOVERY_ENABLED`、`K8S_BACKEND` | 自动 discovery 只在 enabled 且 live K8s 时运行。 |
| poll skipped/locked | `ALERT_SOURCE`、poll scope、Redis lock | 未启用 poll、无有效 filter scope、同 scope 已有 poll。 |
