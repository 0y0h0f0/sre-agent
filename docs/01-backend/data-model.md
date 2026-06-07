# 数据模型

## 设计原则

- Pydantic schema 和 SQLAlchemy model 分离。
- Public ID 使用前缀，例如 `inc_`、`run_`、`tool_`、`act_`、`apv_`、`rpt_`。
- 时间字段使用 timezone-aware UTC datetime。
- JSON 业务字段使用 PostgreSQL JSONB，在非 PostgreSQL 测试环境可降级为 JSON。
- Runbook 和 memory embedding 使用 384 维向量；本地测试可用 JSON fallback。
- `agent_runs.state` 只保存展示/调试快照，不能替代 LangGraph checkpoint。

## 核心事故表

### `incidents`

事故主表。关键字段：

- `incident_id`：public ID，唯一。
- `fingerprint`：告警去重键。
- `source`、`service`、`severity`、`alert_name`。
- `status`：`open`、`diagnosing`、`waiting_approval`、`mitigated`、`resolved`、`failed`。
- `starts_at`、`ends_at`。
- `labels`、`annotations`、`raw_payload`。
- `root_cause_summary`。

索引：

- `incident_id` unique。
- `status`。
- `(service, created_at desc)`。
- `(status, severity)`。
- open fingerprint 唯一索引：`status NOT IN ('resolved', 'failed', 'mitigated')`。

### `agent_runs`

Agent 诊断运行表。关键字段：

- `agent_run_id`：public ID，唯一。
- `incident_id`：关联事故。
- `status`：`queued`、`running`、`waiting_approval`、`succeeded`、`failed`、`cancelled`。
- `celery_task_id`。
- `started_at`、`finished_at`、`duration_ms`。
- `model_name`、`prompt_version`。
- `state`：展示快照。
- `checkpoint_thread_id`、`checkpoint_ns`、`latest_checkpoint_id`。
- `error_code`、`error_message`。
- token 和 cache 指标：`total_prompt_tokens`、`total_completion_tokens`、`provider_cache_*`、`app_cache_*`。

### `agent_run_nodes`

节点轨迹表。每个 LangGraph 节点应写入：

- `node_id`
- `agent_run_id`
- `name`
- `status`
- `started_at`、`finished_at`、`duration_ms`
- `input_summary`
- `output_summary`
- `error_message`

### `tool_calls`

工具调用审计表。关键字段：

- `tool_call_id`
- `agent_run_id`
- `node_name`
- `tool_name`
- `input_json`
- `input_summary`
- `output_json`
- `output_summary`
- `status`
- `error_message`
- `duration_ms`
- `cache_key`
- `cache_hit`

### `evidence_items`

证据表。诊断输出必须引用这些 ID 或 Runbook chunk ID。

- `evidence_id`
- `incident_id`
- `agent_run_id`
- `type`
- `source`
- `source_id`
- `title`
- `excerpt`
- `payload`
- `confidence`
- `timestamp`

### `actions`

推荐动作表。

- `action_id`
- `incident_id`
- `agent_run_id`
- `type`
- `risk_level`
- `status`
- `executor`，MVP 默认 `mock`
- `target`
- `params`
- `reason`
- `rollback_plan`
- `execution_result`

### `approvals`

审批表。

- `approval_id`
- `action_id`
- `incident_id`
- `agent_run_id`
- `status`
- `approver`
- `comment`
- `risk_ack`
- `confirm_action_type`
- `confirm_target`
- `requested_at`
- `decided_at`
- `resume_token`
- `email_token`
- `email_token_expires_at`

L3 审批通过时必须保存二次确认字段。

### `incident_reports`

报告表。

- `report_id`
- `incident_id`
- `agent_run_id`
- `version`
- `root_cause`
- `impact`
- `timeline`
- `actions`
- `follow_ups`
- `body_markdown`

唯一约束：`(incident_id, version)`。

## Runbook 与 RAG 表

### `runbook_chunks`

- `chunk_id`
- `document_id`
- `source_path`
- `title`
- `content`
- `content_hash`
- `embedding`：`vector(384)`。
- `embedding_model`
- `tsv_content`
- `language`
- `metadata`

`content_hash` 唯一，用于 reingest 去重。

### `runbook_drafts`

Runbook 草稿表，用于根据事故和反馈生成待审核文档。

- `draft_id`
- `fingerprint`
- `incident_ids`
- `service`
- `incident_type`
- `title`
- `content`
- `front_matter`
- `status`
- `reviewer`
- `review_comment`
- `source_chunk_ids`
- `llm_model`

### `runbook_versions`

Runbook 文档版本表。

- `version_id`
- `document_id`
- `version_number`
- `source_path`
- `content_hash`
- `change_reason`
- `related_incident_id`
- `related_draft_id`
- `diff_from_previous`
- `created_by`

唯一约束：`(document_id, version_number)`。

## Memory 与学习表

### `memory_items`

- `memory_id`
- `scope`：run、incident、service、procedural 等。
- `scope_key`
- `memory_type`
- `content`
- `content_json`
- `embedding`：nullable `vector(384)`。
- `importance`
- `expires_at`
- `source_ref`

### `memory_events`

记录压缩和记忆事件。

- `event_id`
- `agent_run_id`
- `node_name`
- `event_type`
- `before_tokens`
- `after_tokens`
- `compression_ratio`
- `metadata`

### `false_positive_patterns`

NFA 自动降级模式。

- `fingerprint` 唯一。
- `nfa_count`
- `status`
- `first_nfa_at`
- `last_nfa_at`
- `suppressed_at`
- `expires_at`

### `incident_correlations`

跨事故关联。

- `incident_id_a`
- `incident_id_b`
- `correlation_type`
- `similarity_score`

唯一约束：`(incident_id_a, incident_id_b)`。

### `feedback_items`

用户反馈。

- `feedback_type`：root cause correction、action addition/removal、nfa mark 等。
- `original_value`
- `corrected_value`
- `delta`
- `submitted_by`

## 协作与审计表

### `incident_comments`

事故评论，支持 threaded replies 和 mentions。

### `evidence_annotations`

证据标注。

### `audit_logs`

写前审计日志，记录审批、反馈、评论、标注等行为。

### `approval_groups`

审批组，用 service pattern 路由通知。

### `api_keys`

API key metadata 和 hash。raw key 不入库。

### `email_log`

邮件通知日志，记录通知类型、收件人、关联资源、发送状态、重试次数和 provider message id。

## Eval 表

### `eval_runs`

评测运行，包括 suite、model、prompt version、metrics、git commit 和状态。

### `eval_cases`

评测样本结果，包括 fixture、预期根因、实际根因、状态、耗时和错误。
