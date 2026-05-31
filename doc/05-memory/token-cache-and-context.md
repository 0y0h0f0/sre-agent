# Token 缓存、多级记忆与上下文压缩

## 目标

Agent 的诊断流程会反复使用相同的系统提示词、JSON schema、Runbook、工具摘要和历史故障信息。必须通过稳定 prompt 结构、缓存 key、多级记忆和上下文压缩提升 token 缓存命中率，同时避免把无关历史带入当前诊断。

目标指标：

- Provider prompt cache 命中率 >= 60%，完整 eval 后优化到 >= 75%。
- App prompt segment cache 命中率 >= 70%。
- 单次诊断输入 token P95 控制在模型上下文窗口 40% 以内。
- 压缩后证据保留率 >= 95%。
- 历史记忆误用率 <= 5%。

## Prompt 分层

为了提高缓存命中率，prompt 必须按稳定到动态排序：

1. 固定 system prompt。
2. 固定安全边界和动作风险规则。
3. 固定 JSON output schema。
4. 固定诊断流程说明。
5. 相对稳定的服务画像和 Runbook 摘要。
6. 当前 incident alert 摘要。
7. 当前工具证据。
8. 当前审批或执行状态。

前 4 层在大部分请求中完全相同，必须保持文本稳定，不要在里面插入当前时间、随机 ID 或动态证据。

## Cache key 设计

### 应用层 prompt cache key

```text
app_prompt:{model}:{prompt_version}:{static_hash}:{context_hash}:{output_schema_hash}
```

其中：

- `static_hash`：system prompt + guardrail rules + schema。
- `context_hash`：经过排序和压缩后的 evidence、runbook、memory。
- `output_schema_hash`：Pydantic schema 版本。

### Provider prompt cache 与 Segment cache

Provider prompt cache 依赖模型服务商的前缀缓存能力，应用只能通过稳定 prompt 前缀提升命中率，不能用 Redis 命中直接等价替代。

App segment cache 是本系统自己的 Redis 缓存，用于复用静态 prompt、schema、Runbook chunk、service memory 和 evidence summary。

把 prompt 分段缓存：

```text
prompt_segment:system:{prompt_version}
prompt_segment:schema:{schema_name}:{schema_version}
prompt_segment:runbook:{chunk_id}:{content_hash}
prompt_segment:service_memory:{service}:{memory_version}
prompt_segment:evidence_summary:{evidence_hash}
```

## 多级记忆

### L0：Run-local memory

范围：单次 Agent run。

内容：

- 已完成节点摘要。
- 已查询工具结果摘要。
- 已压缩日志摘要。
- 已生成但未执行的 actions。

存储：LangGraph state + Redis short TTL。

TTL：1 小时。

### L1：Incident memory

范围：同一个 incident 的多次 run。

内容：

- 当前 incident 的稳定事实。
- 用户审批决定。
- 工具失败和降级记录。
- 已确认根因和排除项。

存储：PostgreSQL `memory_items`。

TTL：incident resolved 后 7 天。

### L2：Service memory

范围：同一个 service 的历史故障。

内容：

- 历史故障摘要。
- 常见根因。
- 服务依赖。
- 近期变更模式。

存储：PostgreSQL + pgvector。

TTL：30 到 90 天，按 importance 延长。

### L3：Procedural memory

范围：全局诊断流程经验。

内容：

- 高 5xx 先查发布。
- 延迟升高先看下游和 DB 连接。
- 缓存命中率下降时关联 DB QPS。
- OOMKilled 先查内存 limit 和重启事件。

存储：版本化 Markdown 或数据库，不由模型随意改写。

TTL：长期，人工维护。

## 记忆检索

输入：service、alert_name、incident_type、初步证据。

流程：

1. 先读 L0 run-local memory。
2. 读 L1 incident memory。
3. 用 query embedding 检索 L2 service memory top 5。
4. 根据 incident_type 读取 L3 procedural memory。
5. 使用 relevance threshold 过滤。
6. 进入上下文前统一经过 ContextBudgeter。

## 上下文预算

实现 `packages/memory/context_budget.py`：

```python
class ContextBudget(BaseModel):
    total_limit: int
    reserved_for_completion: int
    static_prompt: int
    schema: int
    alert: int
    evidence: int
    runbook: int
    memory: int
    scratchpad: int
```

默认分配：

| 区块 | 比例 |
| --- | --- |
| static prompt + schema | 25% |
| alert + current state | 10% |
| evidence | 30% |
| runbook | 20% |
| memory | 10% |
| scratchpad | 5% |

## 压缩触发时机

必须在以下时机触发上下文压缩：

1. LogsTool 返回超过 20 条日志或超过 3000 token。
2. evidence 总 token 超过 evidence budget 的 80%。
3. Runbook 检索 top chunks 总 token 超过 runbook budget。
4. LangGraph 完成 3 个以上采集节点后，进入 diagnose 前。
5. 审批恢复时，只保留审批前摘要和关键 evidence id。
6. 生成报告前，对完整 run trajectory 做 timeline 压缩。

## 压缩方法

### 规则压缩

适用：指标、结构化日志、trace。

输出：统计值、异常点、样例、evidence id。

### LLM 摘要压缩

适用：大量日志、历史报告。

职责边界：`packages/memory` 只生成 `CompressionPlan`、预算和输出 schema，不直接调用 LLM provider。真正的 LLM 摘要由 `packages/agent` 的压缩节点通过注入的 summarizer adapter 执行，然后把 `CompressedContext` 写回 memory。

要求：

- 使用固定 summarization prompt，且 prompt 位于 `packages/agent/prompts.py`。
- 输出必须保留 evidence id。
- 不允许生成新事实。
- 摘要中必须有 `omitted_count`。

### 层级压缩

长上下文先按来源分组摘要，再生成全局摘要：

```text
logs -> log_summary
metrics -> metric_summary
traces -> trace_summary
runbooks -> runbook_summary
memory -> memory_summary
all summaries -> diagnosis_context
```

## 压缩输出 schema

```python
class CompressedContext(BaseModel):
    summary: str
    retained_evidence_ids: list[str]
    omitted_evidence_ids: list[str]
    before_tokens: int
    after_tokens: int
    compression_ratio: float
    risk_notes: list[str]
```

## 防止错误记忆污染

- 历史记忆必须带 `source_ref` 和 `confidence`。
- 低于 relevance threshold 的记忆不进入 prompt。
- 与当前强证据冲突的记忆只作为“历史可能性”，不能提升根因置信度。
- 模型输出引用历史记忆时必须同时引用当前 evidence。
- 每次评测统计 memory misuse。

## 代码模块

```text
packages/memory/
  token_counter.py
  prompt_cache.py
  segment_cache.py
  memory_store.py
  retriever.py
  context_budget.py
  compressor.py
  context_builder.py
  schemas.py
```

## 测试

- 相同静态 prompt 产生相同 `static_hash`。
- evidence 顺序变化不影响 `context_hash`，需排序后 hash。
- 超预算时触发压缩。
- 压缩后保留 evidence id。
- 不相关 service memory 不进入上下文。
- 审批恢复时不会携带完整旧日志。
