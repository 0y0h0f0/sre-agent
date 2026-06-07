# 多级记忆实现细节

## MemoryStore 接口

```python
class MemoryStore(Protocol):
    def put(self, item: MemoryItemCreate) -> MemoryItem: ...
    def get_by_scope(self, scope: str, scope_key: str, limit: int) -> list[MemoryItem]: ...
    def search(self, query: str, filters: MemoryFilters, top_k: int) -> list[MemoryItem]: ...
    def mark_used(self, memory_id: str, agent_run_id: str) -> None: ...
```

## Memory item 类型

| 类型 | 示例 | 是否 embedding |
| --- | --- | --- |
| `semantic` | 服务依赖、常见故障 | 是 |
| `episodic` | 某次历史 incident 摘要 | 是 |
| `procedural` | 固定诊断流程 | 可选 |
| `summary` | 当前 run 压缩摘要 | 否 |
| `tool_result` | 工具结果摘要 | 可选 |

## 写入时机

- `collect_*` 完成后：写 run-local 工具摘要。
- `diagnose` 完成后：写 incident root cause candidate。
- `generate_report` 完成后：写 service-level episodic memory。
- `approval` 决策后：写 incident memory，避免恢复时丢失人工判断。
- eval 完成后：不自动写长期记忆，避免测试污染。

## 提升策略

不是所有 run-local memory 都进入长期记忆。

提升条件：

- incident resolved。
- root cause confidence >= 0.7。
- evidence 引用完整。
- 用户审批或评测标准答案确认。
- 非 FakeLLM 手动 demo 时需要标注来源。

## 淘汰策略

- 过期 memory 不参与检索。
- importance 低且 30 天未使用的 service memory 可淘汰。
- 与新确认事实冲突的 memory 标记 deprecated，不物理删除。

## ContextBuilder

输入：

```python
class BuildContextInput(BaseModel):
    incident: dict
    evidence: list[EvidenceItem]
    runbook_chunks: list[RunbookChunk]
    memories: list[MemoryItem]
    output_schema: str
    budget: ContextBudget
```

输出：

```python
class BuiltContext(BaseModel):
    messages: list[dict]
    token_usage_estimate: dict
    segment_cache_keys: list[str]
    compressed_context: list[CompressedContext]
```

## Prompt 稳定性规则

- system prompt 文本放在 `packages/agent/prompts.py` 常量中。
- JSON schema 序列化时字段排序固定。
- evidence 按 `type, timestamp, evidence_id` 排序。
- runbook chunks 按 rerank score 后再按 chunk_id 稳定排序。
- memory 按 relevance score 后再按 memory_id 稳定排序。
- 不在 cacheable segment 中放 `now()`。

## 缓存观测

每次构建上下文记录：

- app prompt segment cache hit/miss。
- provider prompt cache hit/miss，如果模型响应返回该指标；否则记录为 unknown。
- before_tokens。
- after_tokens。
- compression reason。
- retained evidence count。

落库到 `memory_events`，同时更新 `agent_runs.provider_cache_hit_count`、`provider_cache_miss_count`、`app_cache_hit_count`、`app_cache_miss_count`。provider 不返回缓存指标时，provider 计数字段不递增，并在 event metadata 中标记 `provider_cache_status=unknown`。
