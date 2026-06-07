# 记忆、缓存与上下文压缩

## 职责边界

`packages/memory` 不直接调用 LLM provider。它只负责：

- token budget。
- segment cache key。
- deterministic compression。
- context build。
- memory schema。
- memory store。

LLM summarization 如需使用，应由 `packages/agent` 通过注入的 summarizer/LLM adapter 调用，并把结果写回 memory。

## Memory Levels

| 等级 | 范围 | 存储 |
| --- | --- | --- |
| L0 | run-local memory | LangGraph state + Redis short TTL |
| L1 | incident memory | PostgreSQL `memory_items` |
| L2 | service memory | PostgreSQL + pgvector |
| L3 | procedural memory | versioned static knowledge / Runbook |

当前 `MemoryStore` 写入 `memory_items`，并支持按 scope、scope_key、memory_type、importance、service 过滤检索。

## `memory_items`

关键字段：

- `memory_id`
- `scope`
- `scope_key`
- `memory_type`
- `content`
- `content_json`
- `embedding`
- `importance`
- `expires_at`
- `source_ref`

搜索优先使用 pgvector cosine distance；pgvector 不可用时降级为 lexical search，并按 importance/created_at 排序。

## Token Budget

`ContextBudgeter` 默认总预算 32,000 tokens。预算分区由 `ContextBudget.with_defaults()` 生成，ContextBuilder 会分别统计：

- `static`
- `alert`
- `evidence`
- `runbook`
- `memory`
- `cross_incident`
- `scratchpad`

Evidence 超过预算 80% 时触发压缩。

## ContextBuilder

输入包括：

- incident/alert。
- evidence。
- runbook chunks。
- memories。
- cross incident。
- output schema。
- budget。

输出：

- `messages`。
- `token_usage_estimate`。
- `segment_cache_keys`。
- `compressed_context`。

排序规则用于稳定 cache：

- evidence 按 type、timestamp、evidence_id 排序。
- runbook 按 score 和 chunk_id 排序。
- memory 按 score/relevance/importance 排序。

## Prompt segment cache

ContextBuilder 生成 segment keys：

- output schema：`prompt_segment:schema:diagnosis:v1`
- Runbook chunk：`prompt_segment:runbook:{chunk_id}:v1`

这些 key 表示 app 层 prompt segment cache，不等同 provider prompt cache。

## Provider cache 与 app cache

必须分开统计：

- provider cache：LLM adapter 从 provider 返回的 cache 信息。
- app cache：工具或 prompt segment 的应用内缓存。

`agent_runs` 中有：

- `provider_cache_hit_count`
- `provider_cache_miss_count`
- `app_cache_hit_count`
- `app_cache_miss_count`

Redis/app cache hit rate 不能当成 provider prompt cache hit rate。

## 压缩触发条件

文档和实现要求压缩在以下场景触发：

- LogsTool 返回超过 20 条日志。
- logs 超过约 3000 tokens。
- evidence 超过 evidence budget 的 80%。
- Runbook chunks 超过 runbook budget。
- 诊断前已有超过 3 个 collection nodes 完成。
- approval resume 不应携带完整旧日志。
- report generation 需要完整 run trajectory compression。

当前 deterministic compressor 实现了：

- log 条数超过 20。
- evidence token 超过预算 80%。
- 按类型压缩。

## 压缩策略

### Logs

保留：

- top error type。
- top stack signature。
- line count。
- error counts。
- 最多 3 条样本。
- omitted count。

### Metrics

保留：

- metric type。
- service。
- min/max/avg/p95/first/last/change_ratio 等 stats。

删除原始点位。

### Traces

保留：

- duration p95。
- downstream services。
- 最多 5 个 slow spans。
- 最多 5 个 error spans。
- omitted count。

### Generic

保留前三项。

## Evidence ID 保留

压缩结果必须记录：

- `retained_evidence_ids`
- `omitted_evidence_ids`
- `risk_notes`
- before/after tokens。
- compression ratio。

报告和诊断摘要应保留可追溯 ID，不能因压缩丢失全部来源。
