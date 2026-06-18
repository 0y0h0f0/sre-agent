# 工具与证据技术深挖

**最后更新：** 2026-06-18

本文从运行时链路解释工具调用如何产生 `ToolResult`、`tool_calls`、`evidence_items`、`evidence_id` 和 verify gate 结果。它补充 [工具层](../03-tools/tool-layer.md)：工具层文档定义接口和后端，本文解释接口在 Agent run 中如何被调用、缓存、审计和持久化。

如果要从后端适配器角度理解 Prometheus、Loki、Trace、Deployment、K8s、DB、`EffectiveConfig`、URL safety 和 read-only live backend，见 [Observability 与后端适配器技术深挖](observability-backend-adapters-deep-dive.md)。如果要从 action capability、executor backend、snapshot、verify gates 和 replan 角度理解执行闭环，见 [执行器、动作能力与验证闭环技术深挖](executor-action-verification-loop-deep-dive.md)。

## 阅读目标

读完本文应能回答：

- 一个 collect 节点如何调用工具并记录 `tool_calls`。
- `ToolResult.evidence` 如何变成 `evidence_items`，以及什么时候没有 evidence row。
- `cache_key`、`cache_hit` 和 AgentRun app cache 指标从哪里来。
- `collect_all_evidence` 为什么可以并行查询但不并发写 DB。
- `verify` gates 如何重新查询 fresh evidence，并把 `evidence_ids` 写回 gate verdict。
- 工具后端降级时，应该先看 `tool_calls` 还是 `evidence_items`。

## 代码入口

| 主题 | 入口 |
|------|------|
| 工具协议 | `packages/tools/base.py` |
| request-local cache | `packages/tools/cache.py` |
| 依赖构造 | `apps/worker/tasks.py` 的 `_build_deps()` |
| tool call 持久化 | `packages/db/repositories/tool_calls.py` |
| evidence 持久化 | `packages/agent/nodes/_persist.py`、`packages/db/repositories/evidence_items.py` |
| 并行证据采集 | `packages/agent/nodes/collect_all_evidence.py` |
| 单个 collect 节点 | `packages/agent/nodes/collect_metrics.py` 等 |
| gap-fill 采集 | `packages/agent/nodes/collect_gap.py` |
| runbook tool | `packages/agent/nodes/retrieve_runbook.py` |
| verify gates | `packages/agent/nodes/verify.py` |
| Agent run 展示 API | `apps/api/services/agent_run_service.py` |

## 核心对象

| 对象 | 模型/结构 | 用途 |
|------|-----------|------|
| `ToolResult` | Pydantic model | 工具统一返回结构，包含 status、data、summary、evidence、cache metadata 和 error。 |
| `ToolCall` | `tool_calls` | 审计工具调用输入、输出、耗时、状态、cache key 和 cache hit。 |
| `EvidenceItem` | `evidence_items` | 可被诊断、报告、前端、评论和工程指标引用的证据行。 |
| `AgentRunNode` | `agent_run_nodes` | 节点级轨迹，记录节点状态、耗时、输入/输出摘要和错误。 |
| `AgentRun` cache counters | `agent_runs` | 保存 provider cache 和 app/tool cache 统计字段。 |

## 端到端路径

```text
_build_deps()
  -> RequestLocalToolCache
  -> MetricsTool / LogsTool / TraceTool / GitChangeTool / K8sDiagnosticsTool / DbDiagnosticsTool / RunbookSearchTool

collect_all_evidence
  -> run collect_metrics/logs/traces/deployment/k8s/db in worker threads
       -> tool.run(query)
       -> capture node_tracer/tool_call_recorder args
  -> merge partial state on main thread
  -> replay tool_call_recorder -> tool_calls
  -> persist_evidence_batch -> evidence_items
       -> write evidence_id back into state evidence

diagnose / report
  -> cite evidence_id values from state evidence

verify
  -> build gate plan from action capability metadata
  -> re-query metrics/logs/k8s/db read-only tools
  -> record verify tool_calls
  -> persist fresh evidence
  -> attach evidence_ids to verify_gates
```

## 1. ToolResult Contract

所有普通诊断工具都返回 `ToolResult`：

| 字段 | 说明 |
|------|------|
| `status` | `succeeded`、`failed`、`degraded`、`timeout`。 |
| `data` | 结构化结果，供节点判断、verify gate 和报告使用。 |
| `summary` | 简短、可审计的摘要，会进入 `tool_calls.output_summary`。 |
| `evidence` | 可持久化证据列表；为空时通常不会产生 `evidence_items`。 |
| `cache_key` | request-local cache key。 |
| `cache_hit` | 是否来自同一次 run 内的工具缓存。 |
| `duration_ms` | 工具调用耗时。 |
| `error_message` | timeout/degraded/failed 原因。 |

工具失败的基本策略是返回结构化降级结果，而不是让 worker 崩溃。节点可以继续诊断，但要让 `tool_calls.status` 和 `error_message` 留下可查记录。

## 2. Cache Key and Cache Counters

`RequestLocalToolCache` 是单次 Agent run 内的内存缓存：

- 最大 200 条。
- 线程安全，供 `collect_all_evidence` 并行查询共享。
- `get()` miss 时增加 `miss_count`。
- `get()` hit 时返回 `cache_hit=true` 的 copy，并增加 `hit_count`。
- `set()` 保存 `cache_hit=false` 的结果。

当前使用 request-local cache 的工具：

| 工具 | bucket | datasource 入 key | 成功/降级是否缓存 |
|------|--------|-------------------|------------------|
| metrics | 60 秒 | 否 | `succeeded` / `degraded` |
| logs | 60 秒 | 否 | `succeeded` / `degraded` |
| traces | 300 秒 | 是，按 backend name | `succeeded` / `degraded` |
| deployment/git changes | 600 秒 | 是，按 backend name | `succeeded` / `degraded` |
| runbook_search | 无时间桶 | 不适用 | 仅 `succeeded` |

K8s diagnostics 和 DB diagnostics 当前不接入 `RequestLocalToolCache`。它们仍遵守 `ToolResult` 协议，并通过 `tool_calls` 审计。

`AgentRun.app_cache_hit_count` / `app_cache_miss_count` 来自 `RequestLocalToolCache`。这不是 provider prompt cache。provider cache 字段来自 `state["llm_calls"]` 中的 provider usage metadata。

## 3. ToolCall Audit Path

`_build_deps()` 构造 `tool_call_recorder()`，最终调用 `ToolCallRepository.create()` 写入 `tool_calls`：

| 字段 | 来源 |
|------|------|
| `tool_call_id` | `new_id("tool_")` |
| `agent_run_id` | 当前 run |
| `node_name` | 调用节点，例如 `collect_metrics`、`retrieve_runbook`、`verify` |
| `tool_name` | 工具实例的 `name` |
| `input_json` | Pydantic query 的 `model_dump(mode="json")` |
| `input_summary` | 节点提供的简短输入摘要 |
| `output_json` | `ToolResult.model_dump(mode="json")` |
| `output_summary` | `ToolResult.summary` |
| `status` | `ToolResult.status` |
| `error_message` | `ToolResult.error_message` |
| `duration_ms` | `ToolResult.duration_ms` |
| `cache_key` / `cache_hit` | `ToolResult` cache metadata |

Agent Run API 会读取 `tool_calls` 并返回摘要给前端。它不会把每个工具的大型 payload 展平成页面字段；详细输出保留在 DB 的 `output_json`。

## 4. Evidence Persistence Path

工具返回的 evidence 先进入 state，例如：

- `metrics_evidence`
- `logs_evidence`
- `traces_evidence`
- `deployment_evidence`
- `k8s_evidence`
- `db_evidence`
- `verify_evidence`

`persist_evidence()` / `persist_evidence_batch()` 会：

1. 为每条 evidence 创建 `EvidenceItem(evidence_id="evi_*")`。
2. 写入 `incident_id`、`agent_run_id`、`type`、`source`、`source_id`、`title`、`excerpt`、`payload`、`confidence`。
3. 把 DB 生成的 `evidence_id` 回写到原始 state evidence dict。
4. 再把包含 `evidence_id` 的 dict 写回 `EvidenceItem.payload`。
5. `flush()`，由外层 worker 事务统一提交。

如果 `persist_evidence_batch()` 失败，`collect_all_evidence` 会清掉已经回填到 state 的 `evidence_id`，避免后续诊断或报告引用不存在的 DB 行。

## 5. Parallel Collection Boundary

`collect_all_evidence` 并行运行 6 个 collector：

```text
metrics, logs, traces, deployment, k8s, db
```

关键边界：

- worker thread 只执行工具查询和捕获回调参数。
- worker thread 不使用共享 SQLAlchemy session 写 DB。
- 主线程合并 partial state。
- 主线程 replay `node_tracer` 和 `tool_call_recorder`。
- 主线程一次性 `persist_evidence_batch()`。

这避免了共享 DB session 跨线程写入，同时保留并行工具查询带来的速度收益。

K8s 和 DB collector 是条件采集：

- 没有注入对应工具时返回空 evidence。
- alert 与该层无关时返回空 evidence。
- P0/SEV1/CRITICAL 会更积极采集。

空 evidence 不代表工具失败；要结合 node trace 和 `tool_calls` 判断该 collector 是跳过、降级还是失败。

## 6. Collector Fallback Evidence

各 collect 节点在工具返回空 `evidence` 时，通常会构造一条轻量 fallback evidence，保留状态和摘要，例如 metrics/logs/traces/deployment 的 degraded 摘要。

但最终是否有 `evidence_items` 取决于 state 中是否存在 evidence item：

- 工具返回 `succeeded` 且有 evidence：会持久化。
- 工具返回 `degraded` 且节点构造 fallback evidence：会持久化。
- collector 被相关性 gating 跳过：没有 evidence row。
- 工具返回空 evidence 且节点不构造 fallback：没有 evidence row。

因此排查工具可用性时优先看 `tool_calls`；排查诊断引用和报告证据时看 `evidence_items`。

## 7. Gap Collection

`collect_gap` 用 LLM 诊断中的 `missing_evidence` 关键词匹配工具，再用扩展时间窗口重新查询：

- metrics
- logs
- traces
- deployment
- k8s
- db

当前实现会把 gap evidence 标记为 `_collected_in_gap=true` 并持久化，再追加到对应 state evidence 列表。它有 `MAX_DIAGNOSE_CYCLES = 1`，避免诊断无限补证据。

当前实现细节：`collect_gap` 会写 node trace 和 evidence rows，但 safe query helper 没有逐条调用 `tool_call_recorder()`。调试 gap-fill 查询时，应优先查看 `collect_gap` node trace、state evidence 中的 `_collected_in_gap`、以及 `evidence_items`。

## 8. Runbook Search Tool

`retrieve_runbook` 通过 `RunbookSearchTool` 调用 RAG retriever：

- query 使用 alert name、service 和 `top_k=5`。
- tool call 会写入 `tool_calls`。
- chunk 结果写入 `state["runbook_context"]`，不是 `evidence_items`。
- `RunbookSearchTool.evidence` 中包含 runbook source/chunk 信息，但主路径使用 `data["results"]` 作为上下文。

诊断和报告引用 runbook 时应保留 `chunk_id` 或 source path；执行权限仍由 guardrail/approval 决定，runbook 不授予动作执行许可。

## 9. Verify Gates and Fresh Evidence

`verify` 只在 L2/L3 action 执行后运行实质检查；L0/L1 only 会返回 `skipped`。

Gate plan 来自动作 capability metadata：

- 没有 capability gates 时使用默认 `metrics_logs`。
- `k8s_rollout` 按 action target 去重。
- `db_readonly` 默认 optional。
- action params 中的 `required_verify_gates` 只能把 optional gate 升级为 required，不能把 required gate 降级。

当前 gate：

| Gate | 工具 | 读写边界 |
|------|------|----------|
| `metrics_logs` | MetricsTool + LogsTool | 只读 Prometheus/Loki |
| `k8s_rollout` | K8sDiagnosticsTool | 只读 rollout status 或 StatefulSet status |
| `db_readonly` | DbDiagnosticsTool | 只读 connection_pool diagnostics |

每个 gate 返回：

- `gate`
- `required`
- `action_type`
- `target`
- `action_id`
- `verdict`
- `status`
- `summary`
- `evidence_ids`

`verify` 会把 fresh evidence 立即持久化，并把新 `evidence_id` 列表写回对应 gate verdict。`verify_evidence` 也会进入报告生成输入。

## 10. Degraded Results

降级不是失败退出；它是可审计的“不完整上下文”：

- `UnavailableTool` 返回 `status="degraded"` 和明确原因。
- metrics/logs/traces/git 对 timeout 返回 `timeout`，对 HTTP/解析问题返回 `degraded`。
- K8s 非只读 operation 返回 `failed`。
- DB diagnostics 所有 live SQL 都是预定义 SELECT；异常返回 `degraded`。
- verify required gate 的 `degraded` / `unknown` 会阻止整体结果变成 `resolved`。
- verify optional gate 的 `unknown` 不阻止 resolved；如果 optional gate 实际返回 `degraded` 或 `unchanged`，会参与整体判定。

这也是为什么报告和工程指标会同时看 `tool_calls` 和 `evidence_items`：前者反映工具运行健康，后者反映可引用证据覆盖。

## 11. Debug Checklist

Agent Run 页面没有工具调用：

- 查 `tool_calls` 是否为空。
- 查 `tool_call_recorder` 是否被构造进 `AgentDeps`。
- 查 `collect_all_evidence` 是否在线程阶段抛异常，导致 replay 之前失败。

诊断或报告缺少 evidence ID：

- 查 `evidence_items` 是否有当前 `agent_run_id` 的行。
- 查 state evidence 是否被 `persist_evidence_batch()` 回填 `evidence_id`。
- 查 worker logs 是否出现 `persist_evidence_batch` 错误。
- 查 compression 是否保留了 `retained_evidence_ids`。

工具 cache 命中率异常：

- 查 `tool_calls.cache_key` 是否稳定。
- 查 trace/git 的 key 是否包含 datasource。
- 查查询窗口是否落入同一个 UTC bucket。
- 查 K8s/DB diagnostics 是否被误认为应该走 request-local cache。

verify 没有 resolved：

- 查 `verify_gates` 中哪个 required gate 是 `degraded`、`unchanged` 或 `unknown`。
- 查 `verify` 节点下的 `tool_calls`。
- 查 `verify_evidence` 是否有 `_verify_fresh=true`。
- 查 action capability metadata 是否把 `db_readonly` 升级为 required。

## 不要破坏的边界

- 不要在普通 diagnostics tool 中加入真实写操作。
- 不要让 worker thread 使用共享 DB session 写 `tool_calls` 或 `evidence_items`。
- 不要把 raw secret、auth header 或大块 raw logs 放入 evidence payload、tool summary、state 或 prompt。
- 不要把 `tool_calls` 当成可引用证据；诊断和报告应引用 `evidence_id` 或 runbook `chunk_id`。
- 不要把 request-local tool cache 指标当成 provider prompt cache。
- 不要让 verify gate 触发新的 remediation；verify 只能读。

## 相关测试入口

按变更范围选择测试：

- `tests/unit/test_tools.py`
- `tests/unit/test_tools_phase2.py`
- `tests/unit/test_collect_all_evidence.py`
- `tests/unit/test_tool_call_repository.py`
- `tests/unit/test_evidence_validation.py`
- `tests/unit/test_k8s_diagnostics_resilience.py`
- `tests/unit/test_action_capabilities.py`
- `tests/unit/test_agent_nodes.py`
- `tests/integration/test_worker_tool_audit.py`
- `tests/integration/test_graph_flow.py`
- `tests/integration/test_engineering_metrics_api.py`

示例命令：

```bash
pytest tests/unit/test_tools.py tests/unit/test_tools_phase2.py tests/unit/test_collect_all_evidence.py -v
pytest tests/integration/test_worker_tool_audit.py tests/integration/test_graph_flow.py -v
```
