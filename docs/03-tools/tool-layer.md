# 工具层

**最后更新：** 2026-06-15

## 概述

工具层位于 `packages/tools/`，负责把 Prometheus、Loki、Trace、Deployment、Kubernetes、Database、Runbook RAG 和 executor backend 封装成可测试、可降级的接口。Agent 节点只依赖 `BaseTool.run(query)` 或 `ExecutorBackend` 协议，不直接操作外部系统。

默认本地/CI 路径使用 fixture 或 unavailable backend，避免真实外部写入。只有显式配置的 live/read backend 才会访问真实系统。

## 模块地图（15 个模块）

| 模块 | 职责 |
|------|------|
| `base.py` | `BaseTool` 协议、`ToolResult`、计时和摘要 helper |
| `cache.py` | request-local 工具缓存、稳定 cache key、UTC 时间桶 |
| `metrics.py` | Prometheus range query、PromQL 模板、统计聚合 |
| `logs.py` | Loki query_range、LogQL selector fallback、日志聚合 |
| `traces.py` | TraceTool 通用分析路径，提取慢 span、错误 span、下游服务 |
| `trace_backends.py` | fixture、disabled、Jaeger、Tempo trace backend |
| `git_changes.py` | deployment change 工具，统一 GitHub/Argo/fixture 输出 |
| `deployment_backends.py` | fixture、GitHub、Argo CD 只读 deployment backend |
| `k8s.py` | Kubernetes read-only diagnostics tool 和 backend |
| `db_diagnostics.py` | PostgreSQL read-only diagnostics tool 和 backend |
| `runbook_search.py` | Runbook RAG 的 tool wrapper |
| `executor_backends.py` | fixture executor 和 opt-in live K8s executor |
| `mock_executor.py` | legacy mock result map，供兼容路径使用 |
| `unavailable.py` | backend 未配置时返回 degraded 的占位工具 |
| `__init__.py` | 工厂函数导出 |

## Tool 协议

所有普通工具实现同步协议：

```python
class BaseTool(Protocol):
    name: str
    timeout_seconds: float

    def run(self, query: BaseModel) -> ToolResult:
        ...
```

`ToolResult` 字段：

| 字段 | 含义 |
|------|------|
| `status` | `succeeded`、`failed`、`degraded`、`timeout` |
| `data` | 工具结构化数据，用于节点逻辑 |
| `summary` | 审计友好的简短摘要 |
| `evidence` | 可持久化证据列表，后续会获得 evidence ID |
| `cache_key` | 命中的稳定 key |
| `cache_hit` | 是否来自 request-local cache |
| `duration_ms` | 调用耗时 |
| `error_message` | 降级、失败或超时原因 |

新增工具必须有 Pydantic query schema、结构化 result、timeout、降级行为、cache key 策略、审计摘要和 mocked tests。

## 缓存规则

`RequestLocalToolCache` 是单次 agent run 内的内存缓存，最多 200 条，线程安全，供 `collect_all_evidence` 并行调用共享。

| 工具 | 时间桶 | datasource 是否入 key | 说明 |
|------|--------|-----------------------|------|
| metrics | 60 秒 | 否 | query schema 标准化后 hash |
| logs | 60 秒 | 否 | 空 keywords 不参与 hash |
| traces | 300 秒 | 是 | `fixture`、`jaeger`、`tempo` 不共享缓存 |
| deployment/git changes | 600 秒 | 是 | `fixture`、`github`、`argocd` 不共享缓存 |
| runbook_search | 无时间桶 | 不适用 | query/service/incident_type/top_k hash |

cache key 统一使用 UTC bucket。工具缓存命中率是应用层/tool cache 指标，不是 provider prompt cache 指标。

## 工具清单

### MetricsTool

- 文件：`metrics.py`
- Query：`MetricsQuery(service, metric_type, start, end)`
- Backend：Prometheus HTTP API
- 失败行为：timeout 返回 `timeout`；HTTP/解析错误返回 `degraded`
- 安全策略：按窗口 shard，限制 `max_window_seconds`、`max_shards` 和 step
- metric types：`latency`、`error_rate`、`qps`、`cpu`、`memory`、`db_connections`、`cache_hit_rate`、`cpu_throttle`、`disk_avail`、`cert_expiry_days`、`dns_error_rate`、`queue_lag`、`rate_limit_hits`、`slo_burn_rate`

### LogsTool

- 文件：`logs.py`
- Query：`LogsQuery(service, start, end, keywords, limit)`
- Backend：Loki HTTP API
- 失败行为：timeout 返回 `timeout`；HTTP/解析错误返回 `degraded`
- 安全策略：limit 限制为 1 到 1000；keywords 最多取前 10 个；LogQL selector 尝试 service/app/job/container/deployment/pod 等 label fallback
- 输出：error type counts、top stack signature、最多 5 条 samples

### TraceTool

- 文件：`traces.py`、`trace_backends.py`
- Query：`TraceQuery(service, start, end, min_duration_ms=500)`
- Backend：`disabled`、`fixture`、`jaeger`、`tempo`
- 失败行为：backend 空结果为 `degraded`；timeout 为 `timeout`；HTTP/解析错误为 `degraded`
- 输出：span count、slow spans、error spans、downstream services、duration p95

Trace backend 选择：

| 配置 | 行为 |
|------|------|
| `TRACE_ENABLED=false` | `DegradedTraceBackend`，TraceTool 降级 |
| `TRACE_BACKEND=disabled` | 同上 |
| `TRACE_BACKEND=fixture` | 读取 `demo/faults/traces.json` |
| `TRACE_BACKEND=jaeger` | Jaeger-compatible `/api/traces` |
| `TRACE_BACKEND=tempo` | Native Tempo API，带 capability flags |

### GitChangeTool

- 文件：`git_changes.py`、`deployment_backends.py`
- Query：`GitChangeQuery(service, start, end)`
- Backend：`fixture`、`github`、`argocd`
- 失败行为：空结果为 `degraded`；timeout 为 `timeout`；HTTP/解析错误为 `degraded`
- 输出：最多 10 条部署变更，字段统一为 service、deployed_at、commit_sha、author、summary、files

GitHub backend 先读 deployments API；如果没有 deployment records，再按时间窗口查 commits 并过滤与 service 相关的文件。Argo CD backend 只读 application sync history。

### K8sDiagnosticsTool

- 文件：`k8s.py`
- Query：`K8sQuery(service, operation, namespace, pod)`
- Backend：`fixture`、`live`
- 允许操作：`describe_pod`、`logs`、`events`、`rollout_status`、`get_deployment`
- 禁止行为：任何非 read-only operation 直接返回 `failed`

live backend 只调用 Kubernetes read API。它不执行 restart、scale、rollback、cordon、drain 等写操作。`build_remediation_suggestions()` 只生成 dry-run command suggestion，不执行。

`verify` 节点的 `k8s_rollout` gate 使用 `operation="rollout_status"` 重新读取 Deployment rollout 状态；该 gate 仍只读，失败会阻止整体验证结果变成 `resolved`。

### DbDiagnosticsTool

- 文件：`db_diagnostics.py`
- Query：`DbDiagnosticsQuery(operation, limit)`
- Backend：`fixture`、`live`
- 允许操作：`connection_pool`、`locks`、`slow_queries`
- SQL：固定 SELECT 模板，用户输入只选择 operation 和 limit
- live 安全策略：专用连接、`conn.read_only = True`、`statement_timeout`、`_assert_read_only()` 二次校验

live DB diagnostics 只能读取 PostgreSQL 诊断视图，不允许任何 DDL/DML。

`verify` 节点的 `db_readonly` gate 使用 `operation="connection_pool"` 重新读取连接池状态；该 gate 默认 optional，不可用时记录 `unknown`，不会触发任何 DB 写 remediation。

### RunbookSearchTool

- 文件：`runbook_search.py`
- Query：`RunbookSearchQuery(query, service, incident_type, top_k)`
- Backend：`RunbookRetriever`
- 输出：tool evidence type 为 `runbook`，包含 `chunk_id`、`source_path`、metadata 和 score
- 失败行为：任何 retriever 异常返回 `degraded`，不阻塞诊断

### Executor Backends

Executor 不是普通 `BaseTool`，而是 `ExecutorBackend` 协议：

```python
class ExecutorBackend(Protocol):
    name: str
    def execute(self, action: dict, context: ExecutionContext) -> ExecutionResult: ...
    def rollback(self, action: dict, snapshot: dict, context: ExecutionContext) -> ExecutionResult: ...
```

| Backend | 默认 | 行为 |
|---------|------|------|
| `FixtureExecutorBackend` | 是 | 返回确定性 mock result，供测试、本地 demo、CI 使用 |
| `LiveK8sExecutorBackend` | 否 | `EXECUTOR_BACKEND=live` 显式 opt-in，只支持 restart/scale/rollback 类 Kubernetes mutation |

live executor 当前支持：`restart_pod`、`restart_service`、`scale_deployment`、`scale_back`、`rollback_release`。`rollback_deployment` 是兼容别名，会规范化为 `rollback_release` 并调用同一个 Deployment rollback subresource。其它动作失败关闭。

Live K8s action capability metadata 会声明执行后必须运行的 verify gates。Restart/scale 类能力至少包含 `k8s_rollout` 和 `metrics_logs`；rollback 类能力还包含 `db_readonly`。Gate 执行由 Agent `verify` 节点完成，不由 executor backend 直接执行。

## Worker 中的依赖构造

`apps/worker/tasks.py` 的 `_build_deps()` 会：

1. 读取已发布 EffectiveConfig 和 settings。
2. 构造 `RequestLocalToolCache`。
3. 根据 backend 配置构造 trace、deployment、k8s、db diagnostics、executor backend。
4. 如果 metrics/logs URL 不可用，使用 `UnavailableTool` 返回 degraded，而不是传入 `None`。
5. 构造 `RunbookRetriever(use_hybrid=settings.runbook_hybrid_search_enabled)` 和 `RunbookSearchTool`。
6. 把所有依赖注入 `AgentDeps`。

节点不得绕过 `_build_deps()` 直接创建 live client。

## 新增工具 checklist

1. 新建 Pydantic query schema，并验证必填字段、时间窗口、limit。
2. 返回 `ToolResult`，不要返回裸 dict。
3. 给每个外部调用设置 timeout。
4. 明确 degraded/timeout/failure 的返回行为。
5. 设计稳定 cache key，包含 datasource 或 backend 名称，避免跨 backend 污染。
6. evidence 中只放摘要和可追溯 ID，不放 raw secret 或大块 raw logs。
7. 所有 live/read backend 都要有 fixture 或 mock 测试。
8. 如果工具可能产生写入能力，不应放在普通 diagnostics tool 中；必须走 executor backend、guardrail 和 approval。
9. 更新本文件、相关配置参考和测试策略。

## 常用测试入口

- `tests/unit/test_tools.py`
- `tests/unit/test_tool_cache.py`
- `tests/unit/test_trace_backends.py`
- `tests/unit/test_deployment_backends.py`
- `tests/unit/test_k8s_diagnostics_tool.py`
- `tests/unit/test_db_diagnostics_tool.py`
- `tests/unit/test_live_executor_backend.py`
- `tests/unit/test_runbook_search_tool.py`
- `tests/unit/test_build_deps_integration.py`
