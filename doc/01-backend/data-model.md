# 数据模型设计

## 表清单

- `incidents`
- `agent_runs`
- `agent_run_nodes`
- `tool_calls`
- `evidence_items`
- `actions`
- `approvals`
- `incident_reports`
- `runbook_chunks`
- `memory_items`
- `memory_events`
- `eval_runs`
- `eval_cases`

## incidents

字段：

```text
id PK
incident_id unique index
fingerprint unique partial index where status not in ('resolved', 'failed')
source
service
severity
alert_name
status
starts_at
ends_at
labels jsonb
annotations jsonb
root_cause_summary nullable
created_at
updated_at
```

索引：

- `(service, created_at desc)`
- `(status, severity)`
- `fingerprint` partial unique。

## agent_runs

字段：

```text
id PK
agent_run_id unique
incident_id FK incidents.incident_id
status
celery_task_id nullable
started_at nullable
finished_at nullable
duration_ms nullable
model_name
prompt_version
state jsonb
checkpoint_thread_id nullable
checkpoint_ns default ''
latest_checkpoint_id nullable
error_code nullable
error_message nullable
total_prompt_tokens default 0
total_completion_tokens default 0
provider_cache_hit_count default 0
provider_cache_miss_count default 0
app_cache_hit_count default 0
app_cache_miss_count default 0
created_at
updated_at
```

## agent_run_nodes

用于展示 LangGraph 轨迹。

```text
node_id unique
agent_run_id FK
name
status
started_at
finished_at
duration_ms
input_summary text
output_summary text
error_message text nullable
created_at
```

## tool_calls

```text
tool_call_id unique
agent_run_id FK
node_name
tool_name
input_json jsonb
input_summary text
output_json jsonb nullable
output_summary text nullable
status succeeded|failed|degraded|timeout
error_message nullable
duration_ms
cache_key nullable
cache_hit bool default false
created_at
```

## evidence_items

```text
evidence_id unique
incident_id FK
agent_run_id FK
type metric|log|trace|git|runbook|kubernetes|memory
source
source_id nullable
title
excerpt text
payload jsonb
confidence float nullable
timestamp nullable
created_at
```

要求：诊断结果引用的 evidence id 必须存在。

## actions

```text
action_id unique
incident_id FK
agent_run_id FK
type
risk_level L0|L1|L2|L3|L4
status
executor mock
params jsonb
reason text
rollback_plan text nullable
execution_result jsonb nullable
created_at
updated_at
```

## approvals

```text
approval_id unique
action_id FK
incident_id FK
agent_run_id FK
status waiting|approved|rejected|expired
approver nullable
comment nullable
risk_ack bool default false
confirm_action_type nullable
confirm_target nullable
requested_at
decided_at nullable
resume_token nullable
```

## incident_reports

```text
report_id unique
incident_id FK
agent_run_id FK
version int
root_cause text
impact text
timeline jsonb
actions jsonb
follow_ups jsonb
body_markdown text
created_at
```

## runbook_chunks

```text
chunk_id unique
document_id
source_path
title
content text
content_hash
embedding vector(384)
embedding_model
metadata jsonb
created_at
updated_at
```

约束：`content_hash` 唯一，用于重复导入去重。

向量维度：MVP 固定为 `vector(384)`。FakeEmbedding 必须 deterministic，同一输入在任何测试运行中返回相同 384 维向量；如果未来替换真实 embedding model，必须通过新迁移调整维度或新增列，不能静默复用旧列。

## LangGraph checkpoint tables

使用 LangGraph PostgreSQL checkpointer 管理 checkpoint 表，Python 实现使用 `langgraph.checkpoint.postgres.PostgresSaver`；不手写业务迁移模拟 checkpoint。业务表只保存指针字段：

- `agent_runs.checkpoint_thread_id = agent_run_id`。
- `agent_runs.checkpoint_ns = ''`。
- `agent_runs.latest_checkpoint_id` 保存最近一次 checkpoint id，便于 UI 和调试。

首次启动或迁移时必须执行 checkpointer setup。恢复时使用：

```python
config = {"configurable": {"thread_id": agent_run_id, "checkpoint_ns": ""}}
```

不要只依赖 `agent_runs.state` 恢复审批流程；`state jsonb` 仅作为展示快照和降级排查使用。

## memory_items

用于多级记忆。

```text
memory_id unique
scope global|service|incident|run
scope_key
memory_type semantic|episodic|procedural|summary|tool_result
content text
content_json jsonb nullable
embedding vector(384) nullable
importance float default 0.5
expires_at nullable
source_ref nullable
created_at
updated_at
```

## memory_events

用于上下文压缩和缓存观测。

```text
event_id unique
agent_run_id FK
node_name
event_type cache_hit|cache_miss|compress|promote|evict|retrieve
before_tokens int default 0
after_tokens int default 0
compression_ratio float nullable
metadata jsonb
created_at
```

## SQLAlchemy 生成规则

- `JSONB` 使用 PostgreSQL dialect。
- 时间字段使用 timezone-aware datetime。
- 枚举先使用字符串 enum，便于迁移。
- repository 层提供 `get_by_public_id`，业务层不使用内部自增 id。
- 所有 create 方法接收外部生成的 public id，便于测试断言。
