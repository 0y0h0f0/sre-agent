# Celery 任务设计

## Celery app

位置：`apps/worker/celery_app.py`。

配置：

```python
celery_app.conf.update(
    task_track_started=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    task_time_limit=120,
    task_soft_time_limit=90,
    task_default_retry_delay=5,
    task_serializer="json",
    result_serializer="json",
)
```

## 任务清单

| 任务 | 作用 |
| --- | --- |
| `run_incident_diagnosis` | 执行 LangGraph 诊断 |
| `resume_incident_after_approval` | 审批后恢复流程 |
| `ingest_runbooks` | 导入 Runbook |
| `generate_incident_report` | 生成或重生成复盘报告 |
| `run_eval_suite` | 执行评测套件 |

## `run_incident_diagnosis`

签名：

```python
@celery_app.task(bind=True, autoretry_for=(TransientError,), retry_backoff=True, max_retries=2)
def run_incident_diagnosis(self, incident_id: str, agent_run_id: str) -> dict:
    ...
```

流程：

1. 加载 incident 和 agent_run。
2. 如果 agent_run 已经是 terminal 状态，直接返回，保证幂等。
3. 标记 run 为 `running`。
4. 初始化 LangGraph state。
5. 执行 graph。
6. 若进入审批中断，标记 `waiting_approval`。
7. 成功则标记 `succeeded`。
8. 失败则记录 `error_code`、`error_message`。

## 幂等策略

- `agent_run_id` 是任务幂等 key。
- 每个 action 生成前根据 `(agent_run_id, type, target, params_hash)` 查重。
- 每个 approval 根据 `action_id` 查重。
- report 使用 `(incident_id, agent_run_id, version)` 控制重复。

## 重试策略

可重试：

- Prometheus/Loki 短暂不可用。
- Redis 短暂连接失败。
- LLM 网关超时。
- embedding 请求超时。

不可重试：

- schema 校验失败。
- guardrail 拒绝。
- incident 不存在。
- action 非法状态。

## 与 LangGraph 的关系

LangGraph 必须使用 PostgreSQL checkpointer。Python 实现使用 `langgraph.checkpoint.postgres.PostgresSaver`，`thread_id` 固定为 `agent_run_id`，`checkpoint_ns` 固定为空字符串。`agent_runs.state` 只保存展示快照，不能替代 checkpointer。

Celery 是任务执行器，LangGraph 是业务状态机。Celery 不应知道具体诊断节点，只调用：

```python
runner = IncidentGraphRunner(checkpointer=postgres_saver, ...)
config = {"configurable": {"thread_id": agent_run_id, "checkpoint_ns": ""}}
result = runner.invoke(initial_state, config=config)
```

审批恢复调用：

```python
config = {"configurable": {"thread_id": agent_run_id, "checkpoint_ns": ""}}
runner.resume(approval_decision=decision, config=config)
```

## 测试要求

- eager mode 测试任务逻辑。
- 至少一个真实 worker smoke test。
- 测试重复执行同一 `agent_run_id` 不会重复创建 action。
- 测试 transient error 触发 retry。
- 测试 permanent error 不 retry。
