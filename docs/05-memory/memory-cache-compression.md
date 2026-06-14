# 记忆、缓存与上下文压缩

**最后更新：** 2026-06-14

## 概述

`packages/memory` 负责记忆存储、上下文预算、prompt 组装、确定性压缩和 token 估算。它不直接实例化或调用 LLM provider。需要 LLM 摘要时，应由 `packages/agent` 通过注入的 summarizer/LLM adapter 调用，再把结果写回 memory。

当前实现的压缩是规则型、确定性的，目的是避免大日志和超预算证据直接进入 prompt，同时保留 retained/omitted evidence ID 供审计。

## 模块地图（7 个模块）

| 模块 | 用途 |
|------|------|
| `__init__.py` | memory 包导出 |
| `schemas.py` | `ContextBudget`、`BuildContextInput`、`BuiltContext`、`CompressedContext`、memory item/filter schema |
| `token_counter.py` | 4 字符/token 的 deterministic 估算 |
| `context_budget.py` | token budget 分配和 overflow 检测 |
| `context_builder.py` | prompt messages 组装、segment cache key、runbook/memory/cross incident 裁剪 |
| `compressor.py` | 规则型 evidence 压缩 |
| `memory_store.py` | PostgreSQL/pgvector memory 读写和 lexical fallback |

## 记忆级别

| 级别 | scope | 当前读取上限 | 写入来源 | 用途 |
|------|-------|--------------|----------|------|
| L0 | `run` | 5 | `persist_memory` | 当前 agent run 的局部上下文 |
| L1 | `incident` | 5 | `persist_memory` | 当前 incident 的 episodic memory |
| L2 | `service` | 5 search results | `persist_memory`、`compress_context` | 服务级语义记忆和压缩摘要 |
| L3 | `global` + `memory_type=procedural` | 3 search results | `persist_memory` | 成功低风险动作模式 |

`retrieve_memory` 会依次读取 L0、L1、L2、L3，并合并到 `memory_context`。L2/L3 使用 `MemoryStore.search()`；pgvector 可用时按 embedding cosine distance 排序，不可用或失败时回退到 lexical search。

## MemoryStore 行为

`MemoryStore` 提供：

| 方法 | 行为 |
|------|------|
| `put(MemoryItemCreate)` | 创建 `mem_` ID 的 `memory_items` 记录 |
| `get_by_scope(scope, scope_key, limit)` | 按 importance 和 created_at 排序读取 |
| `search(query, filters, top_k)` | 优先 pgvector，失败后按 query terms 做 lexical fallback |
| `mark_used(memory_id, agent_run_id)` | 当前只刷新 `updated_at` |

`_embed_query()` 使用 `build_embedding_provider(get_settings())` 生成查询向量。默认 fake provider 是 deterministic 512 维；这不是 LLM 调用。

## 写入时机

### `compress_context`

`compress_context` 位于 `diagnose` 后。如果 `build_context` 已产生 `compression_events`，它会将压缩摘要写成 L2 service semantic memory：

- `scope="service"`
- `scope_key=service`
- `memory_type="semantic"`
- `content_json.compression_event=true`
- `importance=max(0.3, 1.0 - compression_ratio)`

该节点失败不终止主流程，只追加 `errors`。

### `persist_memory`

`persist_memory` 位于 `generate_report` 后，best-effort 写入：

| 级别 | 写入内容 |
|------|----------|
| L1 incident | alert、fingerprint、root cause、confidence、actions count、compression flag |
| L2 service | root cause summary、confidence、evidence IDs、report summary |
| L3 global procedural | 成功的 L0/L1/L2 动作模式，最多 3 条 |
| L0 run | run phase、diagnosis completion、start time |

L3 procedural memory 当前只记录 `status` 为 `succeeded`、`executed` 或 `approved`，且风险等级在 L0/L1/L2 的动作。

## Token Budget

默认总预算是 `32_000`，其中 `8_000` reserved for completion，prompt 预算为 `24_000`。`ContextBudget.with_defaults()` 当前分配：

| 段 | 默认 token | 说明 |
|----|------------|------|
| static prompt | 4,500 | system prompt 主体 |
| schema | 1,500 | output schema |
| alert | 2,400 | incident/alert metadata |
| evidence | 7,200 | metrics/logs/traces/deployment/k8s/db evidence |
| runbook | 4,800 | runbook chunks |
| memory | 2,400 | L0-L3 memory context |
| cross incident | 1,200 | 相似 incident context |
| scratchpad | 0 | 当前默认不分配 |

`TokenCounter` 使用 `len(text) // 4` 的启发式估算，空文本为 0，非空至少 1。

## ContextBuilder 输入输出

`ContextBuilder.build(BuildContextInput)` 输入：

- `incident`：含 `_system_prompt`、service、severity、alert、time window
- `evidence`：六类证据合并列表
- `runbook_chunks`
- `memories`
- `cross_incident`
- `output_schema`
- `budget`

输出 `BuiltContext`：

| 字段 | 含义 |
|------|------|
| `messages` | system + user prompt messages |
| `token_usage_estimate` | static、alert、evidence、runbook、memory、cross_incident、scratchpad 用量 |
| `segment_cache_keys` | schema 和 runbook chunk 的稳定 segment key |
| `compressed_context` | 本次 evidence compression events |

ContextBuilder 会按 evidence type、timestamp、evidence ID 排序，保持 prompt 组装稳定，利于缓存和测试断言。

## 当前压缩触发

当前代码会生成 `CompressedContext` 的触发条件：

| 条件 | 实现位置 | 行为 |
|------|----------|------|
| evidence 总 token 超过 evidence budget 的 80% | `ContextBuilder.build()` | 调用 `Compressor.compress_evidence()` |
| 某类 log evidence 数量超过 20 | `Compressor._needs_compression()` | 压缩该 log evidence group |
| 某 evidence group token 超过 evidence budget 的 80% | `Compressor._needs_compression()` | 按类型压缩 |

Runbook chunks 和 memory 超预算时当前是按分配预算裁剪纳入 prompt，不产生 `compression_events`。设计边界仍要求未来扩展时覆盖大日志 token、runbook overflow、approval resume 和报告轨迹压缩；实现这些触发时必须补测试并更新本文件。

## 压缩策略

| Evidence type | 策略 |
|---------------|------|
| `log` | 保留 top error type、top stack signature、line count、error counts、前 3 条 samples |
| `metric` | 保留 metric_type、service、stats 中的 min/max/avg/p95/first/last/change_ratio |
| `trace` | 保留 duration p95、downstream services、前 5 个 slow spans、前 5 个 error spans |
| 其它 | 保留前 3 个 item |

`CompressedContext` 会记录：

- `summary`
- `retained_evidence_ids`
- `omitted_evidence_ids`
- `before_tokens`
- `after_tokens`
- `compression_ratio`
- `risk_notes`

压缩后的 item 会尽量保留 `evidence_id`、`source_id`、`title`、`summary`、`status`、`service`、`timestamp` 等可追溯字段。

## 缓存边界

项目里有三类容易混淆的缓存：

| 类别 | 位置 | 当前含义 |
|------|------|----------|
| 工具 request-local cache | `packages/tools/cache.py` | 单次 agent run 内缓存 metrics/logs/traces/git/runbook tool result；hit/miss 写入 `agent_runs.app_cache_*` |
| Prompt segment key | `ContextBuilder.segment_cache_keys` | schema 和 runbook chunk 的稳定 key，供应用层 prompt segment cache 使用；当前 builder 只生成 key |
| Provider prompt cache | LLM provider 自身 | 只有 provider 明确返回 cache hit 信号时才可统计；不要用 tool/app cache 命中率代替 |

`apps/worker/tasks.py` 的 `_populate_run_metrics()` 会从 `state.llm_calls` 汇总 prompt/completion token 和 provider cache 计数，并从 `RequestLocalToolCache` 写入 app cache hit/miss。

## 与 Agent 的职责边界

- `packages/memory` 不调用 LLM。
- `packages/memory` 可以调用 embedding provider 生成搜索向量；默认 fake provider deterministic。
- `packages/agent/nodes/build_context.py` 负责把 state 转成 `BuildContextInput`。
- `packages/agent/nodes/compress_context.py` 负责把压缩事件写回 memory store。
- `packages/agent/nodes/persist_memory.py` 负责诊断结束后的 L0-L3 memory 写入。
- 大块原始日志不应绕过 `ContextBuilder` 直接进入 prompt。

## 新增 memory/compression 能力 checklist

1. 保持 memory 包 LLM-free。
2. 保留 evidence ID 或明确记录 omitted evidence ID。
3. 新增压缩触发条件必须有单元测试和 run trajectory 断言。
4. 不把 provider prompt cache、tool cache、Redis/app segment cache 混为一个指标。
5. 新 memory scope 或 memory_type 需要同步数据模型、检索逻辑、文档和测试。
6. 如果 embedding 维度变化，先处理数据库 schema 和 fake provider determinism。

## 常用测试入口

- `tests/unit/test_memory_store.py`
- `tests/unit/test_context_budget.py`
- `tests/unit/test_context_builder.py`
- `tests/unit/test_context_compression.py`
- `tests/unit/test_token_counter.py`
- `tests/unit/test_agent_nodes.py`
- `tests/unit/test_agent_run_metrics.py`
