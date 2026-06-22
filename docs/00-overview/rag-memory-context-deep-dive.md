# RAG、记忆与上下文技术深挖

**最后更新：** 2026-06-23

本文沿当前代码路径说明 Runbook RAG、memory store、context builder、context compression 和 cache 指标如何组合到一次诊断 run 中。它补充 [Runbook RAG](../04-rag/runbook-rag.md) 和 [记忆、缓存与上下文压缩](../05-memory/memory-cache-compression.md)：前两者说明模块能力，本文说明跨模块执行路径和调试边界。Runbook draft、version、amendment 的完整生命周期见 [Runbook 草稿、版本与 Amendment 生命周期技术深挖](runbook-draft-version-amendment-lifecycle-deep-dive.md)。

## 阅读目标

读完本文应能回答：

- runbook Markdown 或已发布 draft 如何进入 `runbook_chunks`。
- `retrieve_runbook` 如何通过 `RunbookSearchTool` 返回带 `chunk_id` 的上下文。
- `retrieve_memory` 如何读取 L0-L3 memory，并在 pgvector 不可用时回退。
- `build_context` 如何组装 prompt、裁剪 runbook/memory/cross incident，并触发确定性 evidence 压缩。
- prompt segment key、report input compression 和 Web context cache 分别解决什么问题。
- `compress_context` 和 `persist_memory` 分别写入什么 memory。
- tool cache、prompt segment key、provider prompt cache 为什么不能混为一个指标。

## 代码入口

| 链路 | 当前入口 |
|------|----------|
| Markdown 解析 | `packages/rag/metadata.py` |
| Markdown 分块 | `packages/rag/splitter.py` |
| 本地 runbook ingest | `packages/rag/ingest.py`、`RunbookService.ingest()` |
| draft 发布后入库 | `RunbookService.review_draft()`、`RunbookService._ingest_draft_chunks()` |
| embedding provider | `packages/rag/embedding_factory.py` |
| runbook 检索 | `packages/rag/retriever.py` |
| tool wrapper | `packages/tools/runbook_search.py` |
| Agent runbook 节点 | `packages/agent/nodes/retrieve_runbook.py` |
| Agent memory 节点 | `packages/agent/nodes/retrieve_memory.py` |
| context builder | `packages/memory/context_builder.py`、`packages/agent/nodes/build_context.py` |
| deterministic compression | `packages/memory/compressor.py`、`packages/agent/nodes/compress_context.py` |
| report input compression | `packages/memory/compressor.py::compress_report_inputs()`、`packages/agent/nodes/generate_report.py` |
| Web context cache | `packages/rag/runbook_web_context.py` |
| memory 持久化 | `packages/memory/memory_store.py`、`packages/agent/nodes/persist_memory.py` |
| worker 依赖构造 | `apps/worker/tasks.py` 的 `_build_deps()` |

## 关键数据对象

| 对象 | 表/模型 | 用途 | 当前边界 |
|------|---------|------|----------|
| Runbook chunk | `runbook_chunks` / `RunbookChunk` | 已发布知识 chunk | primary `embedding` 是 512 维 |
| Runbook side embedding | `runbook_chunk_embeddings` / `RunbookChunkEmbedding` | M9 provider/model/dimension 附加向量 | 不替代 primary chunk 表 |
| Runbook draft | `runbook_drafts` / `RunbookDraft` | deterministic/template/LLM 草稿 | LLM 只能写 `pending_review` |
| Runbook version | `runbook_versions` / `RunbookVersion` | 发布版本审计 | 发布后才可 ingest 成 chunk |
| Amendment draft | `amendment_drafts` / `AmendmentDraft` | incident diff/反馈修订提议 | 不自动应用 |
| Memory item | `memory_items` / `MemoryItem` | L0-L3 memory | `embedding` 可空 |
| Built context | `BuiltContext` | prompt messages、token usage、segment keys、compression events | 不调用 LLM |
| Agent run cache counters | `agent_runs` | token/cache 统计 | provider cache 与 app/tool cache 分列 |

## 主执行链路

```text
runbook ingest / draft publish
  -> parse_runbook_markdown
  -> split_markdown_document
  -> embed title + content
  -> runbook_chunks

agent run
  -> retrieve_memory
  -> cross_incident
  -> retrieve_runbook
  -> build_context
  -> diagnose
  -> compress_context
  -> ...
  -> generate_report
  -> persist_memory
```

`packages/memory` 只提供预算、组装、压缩和 memory store。它不直接实例化或调用 LLM provider。`packages/rag` 提供知识检索，不提供 remediation 执行许可。runbook 中出现的操作语句可以被分类和审查，但实际执行权限仍由 guardrail、approval 和 executor backend 决定。

## Runbook 入库

### Markdown ingest

入口包括：

- `POST /api/runbooks/ingest`
- `python -m packages.rag.ingest --path demo/runbooks`
- `RunbookService.ingest()`

当前入库步骤：

1. `parse_runbook_markdown()` 要求 front matter 包含 `service`、`incident_type`、`severity`、`owner`、`updated_at`。
2. `split_markdown_document()` 按 H2 section 分块；默认 `target_tokens=450`、`max_tokens=900`、`overlap_tokens=80`。
3. 每个 chunk 用 `source_path + title + content` 计算 `content_hash`，重复 hash 会跳过。
4. 用 embedding provider 对 `title + content` 生成向量。
5. `RunbookChunkRepository.create_chunk()` 强制校验向量长度为 512，写入 `runbook_chunks`。

如果 embedding provider 构造或调用失败，ingestor 会写入 512 维零向量和 `embedding_model="none"`，保留 lexical/BM25 检索能力。

### Draft 发布入库

`RunbookService.review_draft(status="published")` 会创建 `RunbookVersion`，再调用 `_ingest_draft_chunks()` 把 draft content 解析、分块并写入 `runbook_chunks`。

这条路径同样对 embedding provider 失败做降级。重要边界是：draft 发布是人工审查后的状态转换；LLM 生成的 runbook draft 不会自动发布，也不会直接进入执行路径。

## Embedding 边界

`build_embedding_provider(settings)` 当前支持：

| Provider | 维度 | 行为 |
|----------|------|------|
| `disabled` | 512 | 返回零向量，占位写入 primary `vector(512)`，依赖 lexical/BM25 |
| `fake` | 512 | 默认 deterministic provider，用于本地和测试 |
| `bge_zh` | 512 | 本地 BAAI/bge-small-zh HTTP 服务 |
| `text2vec` | 1024 | 被 primary 路径拒绝，因为当前主表是 512 维 |
| `external` | provider 返回值受控 | 只有 M9、semantic search、external provider 和 URL 都显式开启时才构造，否则降级为 `disabled` |

`runbook_chunk_embeddings` 是 M9 side table，用于记录 provider/model/dimension 附加向量。它不改变当前 `runbook_chunks.embedding` 的 primary 512 维边界。

## Runbook 检索

Worker 在 `_build_deps()` 中构造：

```text
RunbookRetriever(RunbookChunkRepository(db), use_hybrid=settings.runbook_hybrid_search_enabled)
RunbookSearchTool(retriever=..., cache=RequestLocalToolCache)
```

Agent 节点 `retrieve_runbook` 当前使用：

```text
RunbookSearchQuery(query=alert_name, service=service, top_k=5)
```

`RunbookSearchTool.run()` 做三件事：

1. 校验并标准化 `RunbookSearchQuery`。
2. 使用稳定 cache key 查询 request-local tool cache。
3. 调用 `RunbookRetriever.search()`，把结果写入 `ToolResult.data["results"]`，并把 chunk/source 信息放入 `ToolResult.evidence`。

`retrieve_runbook` 记录 tool call，然后把 `data["results"]` 写入 `state["runbook_context"]`。当前节点会把 runbook 命中持久化为 `evidence_items(type=runbook)`，`source_id` 指向 `chunk_id`，`payload.source_path` 保留原始 Markdown 路径，并把生成的 `evidence_id` 回写到 `runbook_context`。诊断和报告引用 runbook 时应同时保留 `evidence_id` 和 `runbook_chunk_ids`。

`RunbookRetriever.search()` 当前流程：

1. 计算 query embedding。
2. 遍历 repository 中的 chunks，按 service 和 incident_type metadata 过滤。
3. 对每个候选计算 vector cosine score 与 lexical overlap score，取较大值。
4. 当 `use_hybrid=true` 时，执行 PostgreSQL BM25 recall，并用 adaptive alpha 融合 BM25 和 vector/lexical score。
5. 截取前 20 个候选，调用 configured reranker。
6. 返回 top_k 个 `RunbookSearchResult`。

`RUNBOOK_HYBRID_SEARCH_ENABLED` 是当前 worker 构造 retriever 的实际开关。`SEMANTIC_RUNBOOK_SEARCH_ENABLED` 是 M9 feature gate/control-plane 语义搜索标志，默认关闭；不要把它理解为普通本地 hybrid 检索的唯一开关。

## Memory 读取

`retrieve_memory` 按固定顺序合并 L0-L3：

| 级别 | 查询 | 当前上限 | 说明 |
|------|------|----------|------|
| L0 run | `get_by_scope("run", agent_run_id)` | 5 | 当前 run 局部上下文 |
| L1 incident | `get_by_scope("incident", incident_id)` | 5 | 当前 incident episodic memory |
| L2 service | `search(alert_name, scope="service", service=service)` | 5 | 服务级语义记忆 |
| L3 procedural | `search(alert_name, scope="global", memory_type="procedural")` | 3 | 成功低风险动作模式 |

`MemoryStore.search()` 先尝试用 embedding cosine distance 排序，且只返回 `embedding is not None` 的 memory。只有向量搜索抛异常时，才回退到按 query terms 做 `ILIKE` lexical fallback。过期 memory 会被过滤。

这个行为的调试含义是：如果 pgvector 可用但某些 memory 没有 embedding，它们不会自动混入向量搜索结果；只有向量路径失败才走 lexical fallback。

## Context 组装

`build_context` 节点从 state 中读取：

- metrics/logs/traces/deployment/k8s/db evidence
- `runbook_context`
- `memory_context`
- `cross_incident_context`
- service、severity、alert、time window

然后构造 `BuildContextInput` 并调用 `deps.context_builder.build()`。

`ContextBuilder.build()` 的当前行为：

- evidence 按 `(type, timestamp, evidence_id)` 排序，稳定 prompt 和缓存行为。
- system message 包含 `SYSTEM_PROMPT` 和 output schema 名称。
- user message 包含 `# Alert`、`# Evidence`、`# Runbook`、`# Memory`、可选 `# Related Incidents`。
- runbook chunks 按 `(score, chunk_id)` 降序排序，并按 `budget.runbook` 截断。
- memories 按 `score`、`relevance` 或 `importance` 降序排序，并按 `budget.memory` 截断。
- cross incident 按输入顺序纳入，并按 `budget.cross_incident` 截断。
- `segment_cache_keys` 当前生成 static prompt、schema 和已纳入 prompt 的 runbook chunk 的版本化 key。
- stable prefix hash 只覆盖 leading system messages 和 prompt/schema version；alert、evidence、runbook、memory、Web context 等动态内容必须留在后续 user message。
- diagnosis schema segment 当前指向 compact internal schema；下游 state/API 仍使用 public `DiagnosisOutput`。

`BuildContextInput` 的 schema 默认使用 `ContextBudget()` 原始字段。`ContextBudgeter.allocate_budget()` 和 `ContextBudget.with_defaults()` 则按总预算百分比分配。当前 agent 节点没有显式传入 `budget.total_limit <= 0`，因此 builder 会使用 `ContextBudget()` 默认字段；维护者调整预算时需要同时看 `schemas.py` 和 `context_budget.py`，避免只改一处。

## Token Budget

`ContextBudget` 原始字段当前为：

| 段 | token |
|----|-------|
| total_limit | 32,000 |
| reserved_for_completion | 8,000 |
| static_prompt | 6,000 |
| schema_tokens | 2,000 |
| alert | 3,200 |
| evidence | 9,600 |
| runbook | 6,400 |
| memory | 3,200 |
| cross_incident | 3,200 |
| scratchpad | 1,600 |

`ContextBudget.with_defaults(32_000)` 会基于 24,000 prompt budget 分配：

| 段 | token |
|----|-------|
| static_prompt | 4,500 |
| schema_tokens | 1,500 |
| alert | 2,400 |
| evidence | 7,200 |
| runbook | 4,800 |
| memory | 2,400 |
| cross_incident | 1,200 |
| scratchpad | 0 |

`TokenCounter` 使用 `len(text) // 4` 的 deterministic 估算，非空文本至少为 1。它不是 provider token 计数。

## Evidence 压缩

当前会产生 `CompressedContext` 的路径是：

```text
ContextBuilder.build()
  -> evidence token 超过 budget.evidence * 0.8
  -> Compressor.compress_evidence()
  -> state["compression_events"]
  -> compress_context
  -> service-scope semantic memory
```

`Compressor.generate_compression_plan()` 会按 evidence type 分组，并在以下条件压缩某个 group：

- `type == "log"` 且 item 数量大于 20。
- 该 group token 估算超过 `budget.evidence * 0.8`。

压缩策略：

| Evidence type | 保留内容 |
|---------------|----------|
| `log` | top error type、top stack signature、line count、error counts、前 3 条 samples |
| `metric` | metric_type、service、stats 的 min/max/avg/p95/first/last/change_ratio |
| `trace` | duration p95、downstream services、前 5 个 slow spans、前 5 个 error spans |
| 其它 | 前 3 个 item |

压缩 item 会尽量保留 `evidence_id`、`source_id`、`title`、`summary`、`status`、`service` 和 `timestamp`。`CompressedContext` 记录 retained/omitted evidence IDs、before/after tokens、compression ratio 和 risk notes。

Runbook chunks、memory 和 cross incident 超预算时当前是裁剪，不产生 `compression_events`。

## Report 输入压缩

`generate_report` 在构造 LLM prompt 或确定性报告前，会调用 `Compressor.compress_report_inputs()`：

| 字段 | 行为 |
|------|------|
| evidence | 最多保留 12 条 compact summary，不携带 raw log message/samples |
| evidence_counts | 按 evidence type 统计 |
| retained/omitted/all evidence IDs | 用于报告 traceability 和审计 |
| runbook_chunk_ids | 从 runbook evidence payload、root cause 和 state 合并 |
| actions | 最多 10 条 compact action trajectory |
| errors | 最多 5 条结构化错误摘要，只记录 node/type/status/error_present |

该路径会追加 `scope="report_generation"` 的 compression event。`LLM_DETERMINISTIC_REPORT_ENABLED=true` 时，报告 LLM 调用被跳过，但仍使用同一套压缩输入、traceability 合并和 append-only report version 逻辑。

## Memory 写入

### compress_context

`compress_context` 在 `diagnose` 后执行。只有存在 compression events 且有 service 时才写入 L2 service memory：

| 字段 | 当前值 |
|------|--------|
| `scope` | `service` |
| `scope_key` | service name |
| `memory_type` | `semantic` |
| `content_json.compression_event` | `true` |
| `importance` | `max(0.3, 1.0 - compression_ratio)` |
| `source_ref` | `incident:{incident_id}` |

该节点失败只追加 `errors`，不终止主流程。

### persist_memory

`persist_memory` 在 `generate_report` 后执行，best-effort 写入：

| 级别 | 写入内容 |
|------|----------|
| L1 incident | alert、fingerprint、root cause、confidence、action count、compression flag |
| L2 service | root cause summary、confidence、evidence IDs、report summary |
| L3 global procedural | 最多 3 个成功的 L0/L1/L2 动作模式 |
| L0 run | phase、diagnosis complete、start time |

L3 procedural memory 只从 `status` 为 `succeeded`、`executed` 或 `approved` 且风险等级为 L0/L1/L2 的 action 生成。L3 不记录 L4，也不让历史成功动作绕过下次 guardrail。

## Cache 指标边界

项目中相关但不同的缓存有三类：

| 类别 | 位置 | 写入/展示 |
|------|------|-----------|
| Tool request-local cache | `packages/tools/cache.py` | 单次 agent run 内复用 tool result；worker 汇总到 `agent_runs.app_cache_*` |
| Prompt segment key | `ContextBuilder.segment_cache_keys` | 当前 builder 生成 static/schema/runbook chunk key，供应用层 prompt segment cache 使用 |
| Provider prompt cache | LLM provider 自身 | 只有 provider 调用结果明确返回 cache hit/miss 时才统计到 `agent_runs.provider_cache_*` |
| Web context cache | `RunbookWebContextBuilder` | 只缓存 Web search draft enrichment 的安全结果 traceability 字段；不会接入 Agent run 诊断，除非以后显式执行 LAT-14 |

不要把 Redis/tool cache hit rate 当作 provider prompt cache hit rate。工程指标中的 `provider_prompt_cache_hit_rate` 与 `app_prompt_segment_cache_hit_rate` 必须分开解释；provider 未返回缓存数据时应保持 `unknown` 或空值语义。

Web context cache 也不是 prompt segment cache。它的 key 只包含 provider、purpose、redacted query hash、allow/block policy hash、HTTPS/redirect policy、budget、recency bucket 和 redaction/cache version；value 只保存 title、original/final URL、snippet、content hash、provider、redaction version 和 retrieved_at，并在 cache hit 时重新执行 URL safety 校验。

## 调试 Checklist

### Runbook 搜不到

- 确认 `runbook_chunks` 是否有目标 `service`、`incident_type` metadata。
- 检查 `content_hash` 是否导致重复 ingest 被跳过。
- 检查 `EMBEDDING_PROVIDER` 是否与 512 维 primary schema 兼容。
- 检查 `RUNBOOK_HYBRID_SEARCH_ENABLED` 和 PostgreSQL tsvector/BM25 是否可用。
- 看 `tool_calls` 中 `runbook_search` 的 query、status、cache key 和 summary。

### Memory 没出现

- 确认 `persist_memory` 是否已在终态 run 后执行。
- 检查 `memory_items.scope`、`scope_key`、`memory_type` 和 `expires_at`。
- L2/L3 搜索若 pgvector 成功，只返回有 embedding 的记录；没有 embedding 的记录不会自动 fallback。
- 看 `retrieve_memory` node trace 的 found count。

### Prompt 太大或证据被压缩

- 看 `build_context` node trace 的 `token_budget`。
- 看 state 中 `compression_events` 的 retained/omitted evidence IDs。
- 日志超过 20 条或 evidence group 超过 evidence budget 80% 时会触发压缩。
- runbook/memory 超预算当前是裁剪，不会产生 compression event。
- report prompt 过大时看 `scope="report_generation"` 的 compression event、`all_evidence_ids`、`retained_evidence_ids` 和 `omitted_evidence_ids`。

### Cache 指标异常

- `app_cache_*` 主要来自 request-local tool cache。
- `provider_cache_*` 只来自 LLM call metadata。
- `segment_cache_keys` 只是稳定 key 输出，不代表已经命中 Redis 或 provider cache。

## 不要破坏的边界

- 不把 RAG 检索结果当成执行许可。
- 不让 LLM draft、amendment 或 runbook action classifier 绕过 Agent guardrail。
- 不把 1024 维 provider 写入 primary `runbook_chunks.embedding`。
- 不使用随机 embedding；测试和本地默认使用 deterministic fake provider。
- 不让 memory 包直接调用 LLM。
- 不把大块原始日志绕过 `ContextBuilder` 放入 prompt。
- 不混淆 provider prompt cache、tool cache 和 app prompt segment cache。
- 不把 M9 外部 embedding/web/LLM 能力写成生产默认开启。
- 不把 Web context 接入 Agent run 诊断；该能力是 LAT-14，必须由用户明确要求并加默认关闭 gate。

## 相关测试入口

- `tests/unit/test_rag.py`
- `tests/unit/test_runbook_draft_ingest.py`
- `tests/unit/test_runbook_action_classifier.py`
- `tests/unit/test_runbook_template_engine.py`
- `tests/unit/test_runbook_tsvector_schema.py`
- `tests/unit/test_semantic_runbook_search.py`
- `tests/unit/test_external_embedding_provider.py`
- `tests/unit/test_llm_runbook_generation.py`
- `tests/unit/test_incident_diff_analysis.py`
- `tests/unit/test_memory.py`
- `tests/unit/test_agent_nodes.py`
- `tests/unit/test_worker_celery_app_metrics.py`
- `tests/integration/test_worker_task.py`
- `tests/integration/test_worker_tool_audit.py`
- `tests/integration/test_engineering_metrics_api.py`
- `tests/e2e/test_m9_semantic_search.py`
- `tests/e2e/test_m9_ai_extensions.py`
