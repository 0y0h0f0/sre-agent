# 数据模型、迁移与持久化技术深挖

**最后更新：** 2026-06-17

本文补充 [数据模型](../01-backend/data-model.md)、[后端架构](../01-backend/backend-architecture.md)、[状态与 ID](../11-reference/status-and-ids.md) 和 [API 控制面与服务层技术深挖](api-control-plane-service-deep-dive.md)。它不重复 32 个模型的完整字段表，而是解释当前代码如何把 ORM 模型、Alembic 迁移、repository、LangGraph checkpoint、pgvector、审计和 API key 持久化边界组合起来。

阅读本文后，应该能回答：

- 新增表或字段时要改哪些代码、迁移、文档和测试。
- 哪些状态和唯一约束承担业务幂等。
- `agent_runs.state`、checkpoint pointer 和 LangGraph PostgresSaver 的边界是什么。
- pgvector 不可用时本地/测试 fallback 如何工作。
- 哪些 repository 负责事务锁、版本号、append-only 和内部提交。

## 代码入口

| 主题 | 代码入口 | 说明 |
|------|----------|------|
| ORM 模型 | `packages/db/models.py` | 32 个 SQLAlchemy 模型、JSON/JSONB fallback、512 维向量类型、索引和唯一约束 |
| DB session | `packages/db/session.py` | engine、pool、`SessionLocal(expire_on_commit=False)`、SQLite fallback |
| Base metadata | `packages/db/base.py` | Alembic 和模型统一 metadata |
| Alembic 迁移 | `migrations/versions/` | 当前 17 个迁移版本，线性 revision 链 |
| Repository | `packages/db/repositories/` | 26 个 repository 模块，封装查询、状态转换、锁和版本读取 |
| API service 事务 | `apps/api/services/` | 多 repository 编排、commit/rollback、Celery 入队、审计 |
| Memory store | `packages/memory/memory_store.py` | L0-L3 memory 读写、向量搜索和 lexical fallback |
| LangGraph checkpointer | `apps/worker/tasks.py`、`packages/agent/runner.py` | worker 构造 PostgreSQL checkpointer，业务表只保存 pointer |

## 持久化分层

当前项目的写路径按下面分工：

```text
API router
  -> service：业务校验、状态转换、事务提交、Celery 入队、审计
  -> repository：SQLAlchemy 查询/写对象/锁
  -> ORM model：表结构、约束、索引
  -> Alembic migration：数据库 schema 演进

Worker task
  -> repository：领取 run、同步状态、写 node/tool/evidence/action/report/memory
  -> LangGraph PostgresSaver：保存 graph checkpoint
  -> ORM/Alembic：业务表只保存 checkpoint pointer
```

关键边界：

- Router 不直接写 SQL，也不直接 commit 业务事务。
- Service 是事务和业务流程边界。跨 repository 的写操作应在 service 或 worker task 中集中提交。
- Repository 封装 SQLAlchemy 查询、`SELECT ... FOR UPDATE`、状态变更和 read model 查询。
- Agent node 不直接创建 DB session。节点通过 `AgentDeps` 接收工具、repository/recorder 或 service 风格依赖。
- LangGraph checkpoint 不是 `agent_runs.state`。`agent_runs.state` 只是前端和排障使用的展示快照。

## 类型与方言

`packages/db/models.py` 通过方言 fallback 让本地测试和 PostgreSQL 共享模型：

| 类型 | PostgreSQL | SQLite/test fallback | 当前用途 |
|------|------------|----------------------|----------|
| `JSONType` | JSONB | JSON | labels、annotations、raw payload、state、tool output、metadata |
| `VectorEmbeddingType` | `vector(512)` | JSON | runbook chunk、memory、embedding side table |
| `TSVectorType` | `TSVECTOR` | Text | runbook 全文检索 |

注意点：

- 当前主 embedding 维度是 512。旧 384 维口径已经由迁移 `4dbe6ecad2b1_switch_embedding_384_to_512.py` 切换。
- `RunbookChunk.embedding` 是非空 512 维向量或 JSON fallback。
- `MemoryItem.embedding` 是 nullable 512 维向量或 JSON fallback。
- `RunbookChunkEmbedding.embedding_vector` 同样使用 512 维向量类型，用于 M9 per-provider embedding side table。
- 测试不能使用随机向量。Fake embedding 必须 deterministic。

## 模型分组

当前共有 32 个 ORM 模型，按业务域分组：

| 分组 | 模型 |
|------|------|
| Incident/run 轨迹 | `Incident`、`AgentRun`、`AgentRunNode`、`ToolCall`、`EvidenceItem` |
| 动作、审批、报告、邮件 | `Action`、`Approval`、`IncidentReport`、`EmailLog` |
| Runbook/RAG | `RunbookChunk`、`RunbookChunkEmbedding`、`RunbookDraft`、`RunbookVersion`、`RunbookFeedbackSummary`、`AmendmentDraft` |
| Memory/Eval | `MemoryItem`、`MemoryEvent`、`EvalRun`、`EvalCase` |
| 反馈、协作、审计、认证 | `FalsePositivePattern`、`IncidentCorrelation`、`FeedbackItem`、`IncidentComment`、`EvidenceAnnotation`、`AuditLog`、`ApprovalGroup`、`ApiKey` |
| Discovery/config/poll | `DiscoveryRun`、`DiscoveryProposal`、`EffectiveConfigVersion`、`DiscoveryOverride`、`AlertPollCursor` |

完整字段见 [数据模型](../01-backend/data-model.md)。Runbook draft/version/amendment 的状态转换和 publish/apply 边界见 [Runbook 草稿、版本与 Amendment 生命周期技术深挖](runbook-draft-version-amendment-lifecycle-deep-dive.md)。Discovery run summary、proposal、capability matrix 和 topology 的持久化边界见 [Discovery、Capability Matrix 与服务拓扑技术深挖](discovery-capability-topology-deep-dive.md)。新增模型时，不能只改 `models.py`，还必须补迁移、repository/service 使用点、测试和文档。

## Public ID 与内部主键

数据库表保留整数 `id` 作为内部主键，API、日志和跨表业务引用使用 public ID：

- `Incident.incident_id`
- `AgentRun.agent_run_id`
- `Action.action_id`
- `Approval.approval_id`
- `IncidentReport.report_id`
- `RunbookChunk.chunk_id`
- `MemoryItem.memory_id`
- 其他前缀见 [状态与 ID](../11-reference/status-and-ids.md)

这种设计让数据库 join 保持简单，同时避免把内部自增 ID 暴露给 API 消费者。新增 public resource 时应：

1. 在 `packages/common/ids.py` 使用稳定前缀生成 ID。
2. 在 ORM 中保留唯一索引或唯一约束。
3. 在 schema/API 文档中使用 public ID。
4. 在 [状态与 ID](../11-reference/status-and-ids.md) 增加前缀说明。

## 核心约束

这些约束承担幂等、版本化和安全边界，不只是数据库优化：

| 约束 | 位置 | 行为 |
|------|------|------|
| open fingerprint partial unique index | `Incident` | 同一 fingerprint 只 deduplicate 未终态 incident；终态包括 `resolved`、`failed`、`mitigated` |
| `(incident_id, version)` unique | `IncidentReport` | report regenerate 创建新版本，不覆盖旧版本 |
| `(document_id, version_number)` unique | `RunbookVersion` | runbook 发布历史可追踪 |
| `content_hash` unique | `RunbookChunk` | runbook ingest 避免重复 chunk |
| `(runbook_chunk_id, provider, model, dimension, text_hash)` unique | `RunbookChunkEmbedding` | per-provider embedding 可重试、可降级、可区分模型 |
| `(incident_id_a, incident_id_b)` unique | `IncidentCorrelation` | 跨 incident 关联不重复 |
| `(filter_hash, fingerprint)` unique | `AlertPollCursor` | Alertmanager poll 游标按查询条件隔离 |
| API key hash 持久化 | `ApiKey.key_hash` | raw key 只创建时返回一次，不落库 |
| 审计 append-only | `AuditLogRepository` | 代码只提供 create/query，不提供 update/delete |

`AuditLog` 的 append-only 目前是业务代码边界。生产数据库若需要强保证，应增加数据库 trigger 或权限策略来禁止 update/delete。

## AgentRun、state 与 checkpoint

`AgentRun` 同时服务三个目的：

| 字段/对象 | 用途 | 是否可作为恢复来源 |
|-----------|------|--------------------|
| `agent_runs.status` | API/worker 判断 run 生命周期 | 否，只是状态机字段 |
| `agent_runs.state` | 前端展示和排障快照 | 否 |
| `checkpoint_thread_id` | LangGraph thread id，当前总是 `agent_run_id` | 是，作为 pointer |
| `checkpoint_ns` | LangGraph namespace，当前为空字符串 | 是，作为 pointer |
| `latest_checkpoint_id` | 最近 checkpoint id，如可用则记录 | 是，作为 pointer |
| LangGraph `PostgresSaver` 表 | graph 状态和 resume 数据 | 是，真实 checkpoint |

Worker runtime config 固定为：

```python
{"configurable": {"thread_id": agent_run_id, "checkpoint_ns": ""}}
```

审批恢复必须使用同一个 config。不能通过 `agent_runs.state` 重建 graph，因为这会绕过 `GraphInterrupt`、可能重复执行危险动作，也会破坏 checkpoint 幂等。

## Repository 事务边界

常规模式是 service/worker 管理事务，repository 只修改对象或返回查询结果：

- `IncidentRepository.get_open_by_fingerprint()` 支撑 alert dedup。
- `AgentRunRepository.get_for_update()` 领取 run，用行锁处理 Celery 至少一次投递。
- `ApprovalRepository.get_for_update()` 锁审批行，避免并发批准/驳回。
- `ApprovalRepository.has_waiting_for_run()` 判断同一 run 是否仍有 sibling approval 等待，决定是否 enqueue resume。
- `IncidentReportRepository.next_version()` 读取最新 report version 并返回下一个版本号。
- `EffectiveConfigRepository.get_latest_published()` 只返回 `status=published` 的最高 `version_number`。

一个重要例外是 `PollCursorRepository`：它的方法会内部 `commit()`。这是 Alertmanager poll 游标的独立状态记录，用于在轮询循环中及时持久化 `last_seen_at`、`missing_rounds` 和 resolved inference。修改该 repository 时要注意它和大事务的边界不同。

## Alembic 迁移链

当前 `migrations/versions/` 有 17 个迁移，revision 链是线性的：

| 顺序 | revision | 文件 | 重点 |
|------|----------|------|------|
| 1 | `c26ca1452607` | `c26ca1452607_0001_initial_schema.py` | 初始 schema，PostgreSQL 启用 pgvector extension，早期 embedding 为 384 维 |
| 2 | `0002_phase3_alerts_email` | `0002_phase3_alerts_email.py` | alert/email 相关字段和表 |
| 3 | `0003_runbook_tsvector` | `0003_runbook_tsvector.py` | runbook `tsv_content`、PostgreSQL trigger/function、SQLite fallback |
| 4 | `0004_runbook_drafts_versions` | `0004_runbook_drafts_versions.py` | runbook draft/version |
| 5 | `0005_runbook_language` | `0005_runbook_language.py` | runbook language metadata |
| 6 | `0006_phase5_feedback` | `0006_phase5_feedback.py` | NFA、correlation、feedback |
| 7 | `0007_phase6_collaboration` | `0007_phase6_collaboration.py` | comments、annotations、audit、approval groups |
| 8 | `0008_phase7_api_keys` | `0008_phase7_api_keys.py` | API key 元数据 |
| 9 | `0009_phase7_evals` | `0009_phase7_evals.py` | eval run/case |
| 10 | `4dbe6ecad2b1` | `4dbe6ecad2b1_switch_embedding_384_to_512.py` | runbook/memory embedding 从 384 切换到 512 |
| 11 | `a1b2c3d4e5f6` | `a1b2c3d4e5f6_discovery_config_models.py` | discovery/config 模型 |
| 12 | `b2c3d4e5f6a7` | `b2c3d4e5f6a7_api_key_scopes.py` | API key scopes |
| 13 | `c3d4e5f6a7b8` | `c3d4e5f6a7b8_alert_poll_cursor.py` | Alertmanager poll cursor |
| 14 | `2e6d6dbb06eb` | `2e6d6dbb06eb_runbook_draft_type_and_source.py` | draft type/source |
| 15 | `3f7e8d9c0a1b` | `3f7e8d9c0a1b_runbook_feedback_models.py` | runbook feedback summary |
| 16 | `d4e5f6a7b8c9` | `d4e5f6a7b8c9_m9_amendment_draft_lifecycle.py` | M9 amendment draft lifecycle |
| 17 | `e5f6a7b8c9d0` | `e5f6a7b8c9d0_runbook_chunk_embeddings.py` | per-provider runbook chunk embedding side table |

新增迁移时：

- 不要改旧迁移来“修正历史”，除非项目明确还未发布该迁移。
- ORM、migration、repository 和文档要同步。
- PostgreSQL 特性要提供 SQLite/test fallback，或明确跳过路径。
- 向量维度、唯一约束、索引名称要和 ORM 保持一致。
- downgrade 若无法完整保留数据，应在迁移注释中说明数据影响。

## Runbook 与 Memory 持久化

Runbook 主路径：

```text
runbook ingest
  -> splitter 生成 chunk
  -> embedding provider 生成 512 维 embedding
  -> RunbookChunkRepository.create_chunk()
  -> runbook_chunks.embedding + metadata 持久化
```

`RunbookChunkRepository.create_chunk()` 会校验 embedding 长度必须为 512。外部 embedding 或语义搜索失败时，M9 路径可以降级；`degraded_runbook_embedding()` 返回确定性的 512 维零向量，避免 ingest 因 embedding provider 暂不可用而完全阻断。

M9 per-provider embedding side table 保存：

- provider/model/dimension
- text hash
- redaction version
- vector backend
- status/error code

这让 external embedding 可以独立回滚或重试，不替换主 `runbook_chunks.embedding` 的基本可用性。

Memory 主路径：

```text
compress_context / persist_memory
  -> MemoryStore
  -> memory_items
  -> pgvector cosine distance search when available
  -> lexical fallback when vector backend unavailable
```

`packages/memory` 不直接调用 LLM provider。需要 LLM 摘要时，由 `packages/agent` 注入 summarizer adapter，写回 memory 的仍是确定边界内的结果。

## Discovery 与 EffectiveConfig

Discovery/config 的数据库边界是：

- `DiscoveryRun` 记录发现运行。
- `DiscoveryProposal` 记录待审或自动应用的配置 diff。
- `EffectiveConfigVersion` 记录 worker 可读取的已发布配置快照。
- `DiscoveryOverride` 记录带 TTL 的人工覆盖。

Worker 只读取 latest published `EffectiveConfigVersion`，不会读取 pending proposal，也不会把 production discovery 发现结果自动发布。配置合并和 URL safety 细节见 [配置、Discovery 与 EffectiveConfig 技术深挖](config-discovery-effective-config-deep-dive.md)。

## 审计、认证与 secret

持久化层的安全边界：

- `ApiKey.key_hash` 保存 SHA-256 hash；raw key 只在创建响应中返回一次。
- `AuditLog.details` 只能写脱敏后的上下文，不写 raw secret、Authorization header 或 provider token。
- `EmailLog` 保存通知元数据、收件人、状态和 provider message id，不应保存敏感邮件凭据。
- M9 external call 的错误、审计和指标要先脱敏再入库。
- `EffectiveConfigVersion.config_snapshot` 不应持久化 raw secret；secret 用 `env:VAR_NAME` 引用。

新增字段如果可能承载 secret，应先明确：

1. 是否真的需要入库。
2. 是否可以只存 hash、引用或脱敏摘要。
3. 是否会进入 audit、prompt、state、report 或 frontend。
4. 是否需要新增 secret leakage 测试。

## 常见调试查询

以下是排障时的观察顺序。具体 SQL 可以按当前数据库客户端改写。

| 问题 | 首看表 | 判断 |
|------|--------|------|
| 告警被错误去重 | `incidents` | fingerprint 是否相同，旧 incident 是否仍是非终态 |
| run 没有执行 | `agent_runs` | status、celery_task_id、error_code、checkpoint pointer |
| 节点失败 | `agent_run_nodes` | failed/degraded node 的 error 和 output summary |
| 工具无数据 | `tool_calls`、`evidence_items` | tool status、cache key、evidence 是否回填 ID |
| 审批后未恢复 | `approvals`、`actions`、`agent_runs` | 是否仍有 waiting approval，action 是否 approved/rejected |
| 报告版本异常 | `incident_reports` | `(incident_id, version)` 是否连续，regenerate 是否新建版本 |
| runbook 搜索异常 | `runbook_chunks`、`runbook_chunk_embeddings` | embedding 维度、status、content_hash、provider/model |
| runbook draft/version/amendment 异常 | `runbook_drafts`、`runbook_versions`、`amendment_drafts`、`audit_logs` | draft 是否 published/rejected，version 是否创建，amendment apply 是否只是元数据 |
| memory 未召回 | `memory_items` | scope/scope_key、expires_at、embedding 是否为空 |
| 配置未生效 | `effective_config_versions`、`discovery_overrides` | latest published、override TTL、版本状态 |
| poll resolved 推断异常 | `alert_poll_cursors` | filter_hash、fingerprint、missing_rounds |

## 新增或修改持久化对象 checklist

1. 更新 `packages/db/models.py`，保持 Pydantic schema 和 ORM 分离。
2. 新增 Alembic migration，并确认 revision 链线性。
3. 为 public resource 分配稳定 public ID 前缀。
4. 补 repository 方法，不把 SQL 写散在 router 或 Agent node。
5. 明确事务归属：service/worker commit，只有有充分理由才让 repository 内部 commit。
6. 若涉及并发决策，使用 `SELECT ... FOR UPDATE` 或唯一约束处理幂等。
7. 若涉及 report/runbook/config 版本，使用不可覆盖的新版本。
8. 若涉及 vector/JSON/TSVector，确认 PostgreSQL 与 SQLite/test fallback。
9. 若涉及 secret、auth、audit 或 M9 external call，补脱敏和 leakage 测试。
10. 更新 `docs/01-backend/data-model.md`、[状态与 ID](../11-reference/status-and-ids.md) 和相关专题文档。

## 验证入口

Codex 按项目约束不直接运行 `pytest`、前端测试、Playwright 或完整测试套件。涉及持久化变更时，建议由开发者本地运行：

```bash
alembic current
alembic upgrade head
pytest tests/unit tests/integration --cov=apps --cov=packages --cov-report=term-missing --cov-report=xml --cov-fail-under=80
```

按变更类型增加更窄的测试：

- 迁移或模型：migration/schema/repository integration tests。
- open fingerprint：alert ingestion dedup tests。
- checkpoint pointer：worker/checkpoint resume tests。
- approval/action：L2/L3/L4 negative tests。
- runbook/vector：embedding dimension、source/chunk ID、semantic fallback tests。
- memory/compression：evidence ID retained、expired memory excluded、lexical fallback tests。
- API key/audit：raw key one-time return、scope、audit redaction tests。
