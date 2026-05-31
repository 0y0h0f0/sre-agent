# 工具层设计

## 代码位置

```text
packages/tools/
  base.py
  metrics.py
  logs.py
  traces.py
  git_changes.py
  runbook_search.py
  action_executor.py
  cache.py
  summarizers.py
```

## Base protocol

```python
class ToolResult(BaseModel):
    status: Literal["succeeded", "failed", "degraded", "timeout"]
    data: dict
    summary: str
    evidence: list[dict] = []
    cache_key: str | None = None
    cache_hit: bool = False
    duration_ms: int
    error_message: str | None = None
```

```python
class BaseTool(Protocol):
    name: str
    timeout_seconds: int
    def run(self, query: BaseModel) -> ToolResult: ...
```

## MetricsTool

输入：

```python
class MetricsQuery(BaseModel):
    service: str
    metric_type: Literal["latency", "error_rate", "qps", "cpu", "memory", "db_connections", "cache_hit_rate"]
    start: datetime
    end: datetime
```

实现：

- 使用固定 PromQL 模板。
- HTTP GET `/api/v1/query_range`。
- 对结果做统计：min、max、avg、p95、first、last、change_ratio。
- 返回摘要和 evidence。

## LogsTool

输入：

```python
class LogsQuery(BaseModel):
    service: str
    start: datetime
    end: datetime
    keywords: list[str] = []
    limit: int = 100
```

实现：

- 使用 Loki `/loki/api/v1/query_range`。
- 基础 LogQL：`{service="checkout-api"}`。
- 关键词过滤：`|= "error"`，多个关键词分批查询，避免 query 过复杂。
- 返回前先做聚合：错误类型计数、top stack signature、样例日志。
- 原始日志不直接进入 LLM，最多保留 5 条样例。

## TraceTool

输入：

```python
class TraceQuery(BaseModel):
    service: str
    start: datetime
    end: datetime
    min_duration_ms: int = 500
```

MVP 实现：读取 demo trace fixture 或 mock HTTP source。

输出：慢 span、错误 span、下游服务、duration p95。

## GitChangeTool

输入：

```python
class GitChangeQuery(BaseModel):
    service: str
    start: datetime
    end: datetime
```

实现：读取 `demo/faults/git_changes.json`，筛选时间窗口。

## ActionExecutorTool

输入：

```python
class ActionExecuteRequest(BaseModel):
    action_id: str
    action_type: str
    target: str
    params: dict
    operator: str
```

MVP 只实现 mock executor：

- 不执行真实 kubectl。
- 不调用真实部署系统。
- 根据 action type 返回 deterministic result。
- 支持模拟 timeout 和 failure。

## 工具缓存

工具层使用两级缓存：

1. request-local cache：同一次 run 内完全相同 query 直接复用。
2. Redis cache：相同 service、time_window、query 在短 TTL 内复用。

cache key：

```text
tool:{tool_name}:{service}:{query_hash}:{start_bucket}:{end_bucket}
```

时间桶规则：

- 统一使用 UTC。
- metrics 和 logs 使用 1 分钟 bucket，`start` 向下取整，`end` 向上取整。
- traces 使用 5 分钟 bucket。
- git changes 使用 10 分钟 bucket。
- `query_hash` 必须基于规范化后的 query schema，字段排序固定，忽略空 keywords。

TTL：

- metrics：60 秒。
- logs：60 秒。
- traces：120 秒。
- git changes：10 分钟。
- runbook search：30 分钟。

## 审计

工具调用完成后由 service 写 `tool_calls`。必须记录：

- tool name。
- input summary。
- output summary。
- status。
- duration。
- cache key。
- cache hit。
- error message。
