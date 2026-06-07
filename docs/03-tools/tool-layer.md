# 工具层

## 通用接口

所有工具遵循 `BaseTool` 协议：

```python
class BaseTool(Protocol):
    name: str
    timeout_seconds: float

    def run(self, query: BaseModel) -> ToolResult:
        ...
```

`ToolResult` 字段：

- `status`：`succeeded`、`failed`、`degraded`、`timeout`。
- `data`：结构化结果。
- `summary`：审计友好的摘要。
- `evidence`：可写入 `evidence_items` 的证据列表。
- `cache_key`。
- `cache_hit`。
- `duration_ms`。
- `error_message`。

工具失败时不应静默吞异常。可返回 timeout/degraded/failed，但关键安全 gate 不在工具层放行。

## 工具清单

| 工具 | 文件 | Query | 默认后端 | Cache bucket |
| --- | --- | --- | --- | --- |
| MetricsTool | `packages/tools/metrics.py` | `MetricsQuery` | Prometheus HTTP | 60 秒 |
| LogsTool | `packages/tools/logs.py` | `LogsQuery` | Loki HTTP | 60 秒 |
| TraceTool | `packages/tools/traces.py` | `TraceQuery` | fixture | 300 秒 |
| GitChangeTool | `packages/tools/git_changes.py` | `GitChangeQuery` | fixture | 600 秒 |
| K8sDiagnosticsTool | `packages/tools/k8s.py` | `K8sQuery` | fixture | 无 request cache |
| DbDiagnosticsTool | `packages/tools/db_diagnostics.py` | `DbDiagnosticsQuery` | fixture | 无 request cache |
| RunbookSearchTool | `packages/tools/runbook_search.py` | Runbook search query | RAG retriever | tool cache |
| Mock executor | `packages/tools/mock_executor.py` | action dict | fixed map | 不适用 |

## Cache key

`build_cache_key()` 会：

- 对 Pydantic query 做 JSON 归一化。
- 移除 `service`、`start`、`end`。
- 将 datasource 纳入 hash，避免 fixture/jaeger/tempo 等后端冲突。
- 按 UTC 时间 bucket 规整 start/end。

格式：

```text
tool:{tool_name}:{service}:{query_hash}:{start_bucket}:{end_bucket}
```

`RequestLocalToolCache` 是单次 agent run 内的内存缓存，记录 hit/miss 数并写入 agent run 的 app cache metrics。

## MetricsTool

`MetricsQuery`：

- `service`
- `metric_type`
- `start`
- `end`

支持 metric type：

- `latency`
- `error_rate`
- `qps`
- `cpu`
- `memory`
- `db_connections`
- `cache_hit_rate`
- `cpu_throttle`
- `disk_avail`
- `cert_expiry_days`
- `dns_error_rate`
- `queue_lag`
- `rate_limit_hits`
- `slo_burn_rate`

工具通过 Prometheus range query 查询数据，输出 stats、sample_count 和 metric evidence。大时间窗会按配置 sharding。

## LogsTool

`LogsQuery`：

- `service`
- `start`
- `end`
- `keywords`
- `limit`

工具查询 Loki，聚合错误类型、样本、行数和摘要。返回 log evidence。若日志过多，后续 context compressor 会保留样本和错误聚合。

## TraceTool

`TraceQuery`：

- `service`
- `start`
- `end`
- `min_duration_ms`

默认 fixture 后端，也可配置 Jaeger/Tempo 读后端。工具提取：

- span count。
- slow spans。
- error spans。
- downstream services。
- duration p95。

Cache bucket 为 5 分钟。

## GitChangeTool

`GitChangeQuery`：

- `service`
- `start`
- `end`

默认 fixture 后端，也可配置 GitHub/Argo CD 读后端。用于定位事故窗口内的部署变更。Cache bucket 为 10 分钟。

## K8sDiagnosticsTool

`K8sQuery`：

- `service`
- `operation`
- `namespace`
- `pod`

只允许 read-only operation：

- `describe_pod`
- `logs`
- `events`
- `rollout_status`

任何非只读 operation 返回 failed，不执行真实写操作。Live backend 仅调用 Kubernetes read API。

## DbDiagnosticsTool

`DbDiagnosticsQuery`：

- `operation`：`connection_pool`、`locks`、`slow_queries`。
- `limit`。

Live backend 使用固定 SELECT 模板，不拼接用户 SQL。它会：

- 使用 dedicated connection。
- 设置 read only。
- 设置 statement timeout。
- 拒绝包含写关键字的 SQL。

## Mock executor

MVP 所有执行动作使用 mock executor。mock executor 是固定结果映射，保证 graph node 和 API action service 的行为一致。

禁止新增真实 executor 作为默认路径。若未来实现真实执行，也必须保持 guardrail、审批和 dry-run/手动确认边界。
