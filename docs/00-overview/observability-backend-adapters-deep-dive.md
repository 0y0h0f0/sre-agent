# Observability 与后端适配器技术深挖

**最后更新：** 2026-06-18

本文从当前代码路径解释 Prometheus、Loki、Trace、Deployment、Kubernetes 和 Database 诊断后端如何接入 Agent。它补充 [工具层](../03-tools/tool-layer.md)、[工具与证据技术深挖](tool-evidence-deep-dive.md)、[后端对接范围](../11-reference/backend-connectivity.md) 和 [配置参考](../11-reference/configuration.md)。

## 阅读目标

读完本文应能回答：

- 一个 worker run 如何选择当前 active observability backend。
- 哪些后端经由 `EffectiveConfig`，哪些仍直接来自 settings。
- fixture、unavailable、degraded、live read-only backend 的区别。
- Prometheus/Loki/Trace/Git 的 request-local cache bucket 如何工作。
- K8s 和 DB live diagnostics 为什么是只读诊断，不是 remediation。
- URL safety、secret redaction 和 discovery review 边界在哪里生效。
- 工具没有数据时应如何从 `tool_calls`、`evidence_items` 和配置路径定位问题。

## 代码入口

| 主题 | 入口 |
|------|------|
| worker 依赖构造 | `apps/worker/tasks.py` 的 `_build_deps()` |
| 工具协议 | `packages/tools/base.py` |
| request-local cache | `packages/tools/cache.py` |
| Prometheus metrics | `packages/tools/metrics.py` |
| Loki logs | `packages/tools/logs.py` |
| Trace 通用分析 | `packages/tools/traces.py` |
| Trace backend | `packages/tools/trace_backends.py` |
| Deployment change tool | `packages/tools/git_changes.py` |
| Deployment backend | `packages/tools/deployment_backends.py` |
| Kubernetes diagnostics | `packages/tools/k8s.py` |
| Database diagnostics | `packages/tools/db_diagnostics.py` |
| backend 未配置占位 | `packages/tools/unavailable.py` |
| EffectiveConfig 合并 | `packages/discovery/config_merge.py` |
| backend endpoint discovery | `packages/discovery/backend_endpoints.py` |
| URL safety | `packages/common/backend_url_safety.py` |
| runtime secret config | `packages/common/backend_auth.py` |

## 当前模型

当前实现是单 Agent 实例对接一套后端环境：

```text
one worker process
  -> one active Prometheus endpoint
  -> one active Loki endpoint
  -> one active trace backend type and URL
  -> one active deployment change backend
  -> one Kubernetes read context
  -> one optional DB diagnostics endpoint
```

这一套后端环境可以覆盖多个业务服务。服务维度来自 alert payload、Prometheus/Loki label、K8s discovery 和 topology，而不是同一个 worker 同时 fan-out 到多套 Prometheus/Loki/trace/K8s 集群。

需要多套后端环境时，推荐部署多个 Agent 实例，每个实例独立配置自己的 backend URL、K8s namespace/RBAC、DB diagnostics DSN 和 executor opt-in。

## 端到端依赖构造

诊断 run 的工具实例在 worker 内构造：

```text
diagnose_incident_task / resume_approval_task
  -> _build_deps(db, settings, agent_run_id, incident_id)
       -> RequestLocalToolCache()
       -> EffectiveConfig.from_demo_sources() or from_operator_sources()
       -> MetricsTool / LogsTool
       -> TraceTool or UnavailableTool
       -> GitChangeTool
       -> K8sDiagnosticsTool
       -> DbDiagnosticsTool
       -> RunbookSearchTool
       -> ExecutorBackend
  -> build_graph()
  -> run_diagnosis_graph()
```

本地和 demo 环境使用 `EffectiveConfig.from_demo_sources(settings)`。生产环境使用 `EffectiveConfig.from_operator_sources(...)`，合并优先级是：

```text
env > active override > profile > published > safe default
```

生产 worker 只读取 latest published `EffectiveConfigVersion` 和 active override，不读取 discovery proposal。Discovery 可以发现并提出候选 endpoint，但不会自动变成 worker 的 active backend。

## EffectiveConfig 与 Settings 边界

并非所有工具都通过同一条配置路径接入，这一点在调试生产环境时很重要。

| 后端 | worker 当前来源 | 说明 |
|------|----------------|------|
| Prometheus metrics | `EffectiveConfig.prometheus.url` | URL 缺失时构造 `UnavailableTool("metrics")`。 |
| Loki logs | `EffectiveConfig.loki.url` | URL 缺失时构造 `UnavailableTool("logs")`。 |
| Jaeger trace | `EffectiveConfig.jaeger.url` + `TRACE_BACKEND=jaeger` | URL 缺失时 trace unavailable。 |
| Tempo trace | `EffectiveConfig.tempo.url` + `TRACE_BACKEND=tempo` | 还受 M9 feature flags 控制。 |
| Fixture trace | `TRACE_BACKEND=fixture` | 非生产可用；生产依赖构造中不会启用 trace fixture。 |
| Deployment changes | `DEPLOYMENT_BACKEND` 等 settings | 当前不走 `EffectiveConfig`。 |
| Kubernetes diagnostics | `K8S_BACKEND`、`K8S_NAMESPACE` 等 settings | 当前不走 `EffectiveConfig`。 |
| Database diagnostics | `DB_DIAGNOSTICS_BACKEND`、`DB_DIAGNOSTICS_URL` 等 settings | 当前不走 `EffectiveConfig`。 |
| Executor backend | `EXECUTOR_BACKEND` 等 settings | 与 diagnostics 分离，默认 fixture。 |

因此，Prometheus/Loki/trace 的生产 URL 主要看 published config 和 active override；GitHub/Argo/K8s/DB/executor 仍主要看运行时 settings。修改配置文档或发布清单时不要把这些路径混在一起。

## ToolResult 统一语义

普通诊断工具都返回 `ToolResult`：

| 字段 | 语义 |
|------|------|
| `status` | `succeeded`、`failed`、`degraded`、`timeout`。 |
| `data` | 节点、verify gate 和报告消费的结构化数据。 |
| `summary` | 写入 `tool_calls.output_summary` 的短摘要。 |
| `evidence` | 可持久化证据列表；为空时通常没有 `evidence_items`。 |
| `cache_key` | 支持缓存的工具返回稳定 cache key。 |
| `cache_hit` | 命中 request-local cache 时为 `true`。 |
| `duration_ms` | 工具调用耗时。 |
| `error_message` | 降级或失败原因，应可审计且不包含 raw secret。 |

重要区别：

- `degraded` 表示工具路径可用但没有足够数据，或后端错误被安全降级。
- `timeout` 表示调用超时，节点可以继续用其他证据诊断。
- `failed` 只用于明确拒绝或不可恢复的工具层错误，例如 K8s diagnostics 收到非只读 operation。
- 没有 `evidence_items` 不等于没有工具调用，应先看 `tool_calls` 的 status、summary、cache 和 error。

## Adapter Matrix

### MetricsTool / Prometheus

| 项目 | 当前行为 |
|------|----------|
| 查询 schema | `MetricsQuery(service, metric_type, start, end)` |
| 后端 URL | worker 中来自 `EffectiveConfig.prometheus.url` |
| HTTP API | Prometheus `/api/v1/query_range` |
| service label | `METRICS_SERVICE_LABEL`，默认 `service` |
| 超时 | `TOOL_TIMEOUT_SECONDS`，默认 2 秒 |
| cache bucket | 60 秒 |
| cache datasource | 无；cache key 由 query、service、时间桶构成 |
| evidence | type=`metric`，source=`prometheus` |

`MetricsTool` 根据 `metric_type` 选择 PromQL 候选表达式。窗口较大时会按 `METRICS_MAX_WINDOW_SECONDS` 和 `METRICS_MAX_SHARDS` 做分片/步长调整，覆盖完整时间范围而不是静默截断。

无样本时返回 `degraded`，不会写 evidence。HTTP 错误、解析错误等也返回 `degraded`。超时返回 `timeout`。成功和 degraded 结果都会写入 request-local cache，供同一个 run 内后续节点复用。

### LogsTool / Loki

| 项目 | 当前行为 |
|------|----------|
| 查询 schema | `LogsQuery(service, start, end, keywords, limit)` |
| 后端 URL | worker 中来自 `EffectiveConfig.loki.url` |
| HTTP API | Loki `/loki/api/v1/query_range` |
| service label | `LOGS_SERVICE_LABEL`，默认 `service` |
| 超时 | `TOOL_TIMEOUT_SECONDS`，默认 2 秒 |
| cache bucket | 60 秒 |
| cache datasource | 无 |
| evidence | type=`log`，source=`loki` |

`LogsTool` 会对 service selector 做 fallback：配置 label、`service`、`app`、`job`、`container`、`deployment`、`app_kubernetes_io_name`、`kubernetes_pod_name`、`pod` 等标签都会尝试匹配。keywords 最多取前 10 个；没有 keyword 时走无 keyword 查询。

结果会聚合 `error_type_counts`、`top_error_type`、`top_stack_signature` 和少量样本。没有日志行返回 `degraded`。HTTP/解析错误返回 `degraded`。超时返回 `timeout`。成功和 degraded 结果会缓存。

### TraceTool / Fixture、Jaeger、Tempo、Disabled

| 项目 | 当前行为 |
|------|----------|
| 查询 schema | `TraceQuery(service, start, end, min_duration_ms=500)` |
| 后端类型 | `TRACE_BACKEND=disabled|fixture|jaeger|tempo` |
| 总开关 | `TRACE_ENABLED=false` 强制 degraded backend |
| cache bucket | 300 秒 |
| cache datasource | backend name，例如 `fixture`、`jaeger`、`tempo` |
| evidence | type=`trace`，source=backend name |

`TraceTool` 负责通用分析：按 service 和时间窗过滤 span，提取慢 span、error span、downstream services 和 p95。具体数据来源由 backend 提供：

| Backend | 行为 |
|---------|------|
| `FixtureTraceBackend` | 读取 `TRACE_FIXTURE_PATH`，默认 `demo/faults/traces.json`。 |
| `DegradedTraceBackend` | 返回空 span，用于 disabled 或不可用路径。 |
| `JaegerTraceBackend` | 调用 Jaeger `/api/traces`，解析 processes、tags、status/http 错误。 |
| `TempoTraceBackend` | 调用 Tempo native API，支持 search、TraceQL 和 trace detail 能力探测。 |

M9 关闭时，`TRACE_BACKEND=jaeger` 保持 M8 已验证行为；`TRACE_BACKEND=tempo` 会被 feature flag 降级，因为 Tempo 属于 M9 默认关闭能力。生产依赖构造中不会启用 `TRACE_BACKEND=fixture`，以免生产误读 demo trace。

### GitChangeTool / Fixture、GitHub、Argo CD

| 项目 | 当前行为 |
|------|----------|
| 查询 schema | `GitChangeQuery(service, start, end)` |
| 后端类型 | `DEPLOYMENT_BACKEND=fixture|github|argocd` |
| cache bucket | 600 秒 |
| cache datasource | backend name |
| evidence | type=`deployment`，source=backend name |

名称保留为 `GitChangeTool`，但当前语义是 deployment change tool。它会统一 fixture、GitHub deployment/commit 和 Argo CD sync history 输出。

| Backend | 行为 |
|---------|------|
| `FixtureDeploymentBackend` | 读取 `GIT_CHANGES_FIXTURE_PATH`，默认 `demo/faults/git_changes.json`。 |
| `GitHubDeploymentBackend` | 使用 GitHub API 只读查询 deployments；没有 deployments 时回退到 commits 和 commit detail，并按 service 相关文件过滤。 |
| `ArgoCDDeploymentBackend` | 读取 Argo CD application sync history。 |

GitHub/Argo token 只在 runtime client 构造中使用，不应进入 prompt、state、audit 或文档示例。没有匹配 change 时返回 `degraded`，不会生成 deployment evidence。

如果需要逐步理解 GitHub deployments、commits fallback、Argo CD sync history 如何归一成 deployment evidence，以及这类证据和 L3 rollback action 的安全边界，见 [Deployment Change、GitHub、Argo CD 与发布变更证据技术深挖](deployment-change-github-argocd-deep-dive.md)。

### K8sDiagnosticsTool / Fixture、Live Read-Only

| 项目 | 当前行为 |
|------|----------|
| 查询 schema | `K8sQuery(service, operation, namespace, pod)` |
| 后端类型 | `K8S_BACKEND=fixture|live` |
| 允许操作 | `describe_pod`、`logs`、`events`、`rollout_status`、`get_deployment`、`get_statefulset` |
| cache | 当前没有 request-local cache |
| evidence | type=`k8s`，source=backend name |

K8s diagnostics 是只读诊断工具，不执行 remediation。`LiveK8sBackend` 只读取 Kubernetes API，并做以下限制：

- namespace、pod、deployment、statefulset 名称先做 DNS/资源名校验。
- pod logs 只读 tail lines，并做文本 redaction。
- events 只保留 message、reason、type、count、时间等可审计字段。
- describe pod 输出 phase、node、container image/ready/restart/state/reason、conditions 等安全摘要。
- deployment/statefulset 输出 spec/status 的安全字段，不暴露 raw env、args、annotations、secret。
- Kubernetes API 错误被映射为 `not_found`、`forbidden`、`unauthorized`、`rate_limited`、`timeout`、`api_error`、`read_failed` 等结构化错误。

如果传入非只读 operation，工具直接返回 `failed`，summary 为拒绝执行，`error_message` 为 `k8s tool is read-only`。这和 executor backend 是两条路径：诊断工具只读，真实 Kubernetes mutation 只能由 opt-in `LiveK8sExecutorBackend` 在 guardrail 和审批之后执行。

### DbDiagnosticsTool / Fixture、Live Read-Only

| 项目 | 当前行为 |
|------|----------|
| 查询 schema | `DbDiagnosticsQuery(operation, limit)` |
| 后端类型 | `DB_DIAGNOSTICS_BACKEND=fixture|live` |
| 允许操作 | `connection_pool`、`locks`、`slow_queries` |
| cache | 当前没有 request-local cache |
| evidence | type=`db`，source=backend name |

live DB diagnostics 只允许预定义 SELECT：

- `connection_pool` 查询 `pg_stat_activity` state 统计。
- `locks` 查询 `pg_locks` mode 统计。
- `slow_queries` 查询 `pg_stat_statements`，按 `mean_exec_time` 排序。

`LiveDbBackend` 在执行前调用 `_assert_read_only(sql)`，拒绝非 SELECT 和 `insert`、`update`、`delete`、`drop`、`alter`、`truncate`、`create`、`grant` 等关键词。连接会设置：

```text
conn.read_only = True
SET statement_timeout = DB_DIAGNOSTICS_STATEMENT_TIMEOUT_MS
```

DSN 缺失时 live backend 构造阶段会抛 `ValueError`，这是配置错误，应在部署检查中前置发现；系统不会补一个危险默认值。backend 已构造后，查询异常会由 `DbDiagnosticsTool.run()` 降级为 `degraded`。没有 rows 返回 `degraded`，不会生成 DB evidence。

### UnavailableTool

`UnavailableTool` 是后端未配置时的安全占位工具。它返回：

```text
status = degraded
duration_ms = 0
summary = "<tool> unavailable: <reason>"
```

它用于避免生产 safe default 下因为 Prometheus/Loki/trace URL 为空导致 worker 构造失败。调用方仍会看到 `tool_calls`，但没有 evidence。

## Cache Bucket 与 Query Hash

request-local cache 当前用于 Metrics、Logs、Trace、Deployment change 等工具。它只在单个 agent run 的依赖对象内有效，不是跨 run 的 Redis 缓存。

| 工具 | Bucket | datasource 进入 hash | 说明 |
|------|--------|----------------------|------|
| MetricsTool | 60 秒 | 否 | 使用 UTC start/end bucket。 |
| LogsTool | 60 秒 | 否 | 空 keyword 会从 normalized query 中移除。 |
| TraceTool | 300 秒 | 是 | 区分 fixture/jaeger/tempo。 |
| GitChangeTool | 600 秒 | 是 | 区分 fixture/github/argocd。 |
| K8sDiagnosticsTool | 无 | 不适用 | 当前每次读取。 |
| DbDiagnosticsTool | 无 | 不适用 | 当前每次读取。 |

`build_cache_key()` 会从 normalized query 中移除原始 `service`、`start`、`end`，改用稳定 service 和 UTC 时间桶字段；如果传入 `datasource`，会折叠进 query hash，避免不同 trace/deployment backend 复用同一个缓存项。

## URL Safety 与 Secret 边界

后端 URL 安全主要在配置/discovery/override 路径生效：

- URL 不允许携带 username/password。
- scheme 只允许 `http` 或 `https`。
- metadata endpoint 永远拒绝，例如 `169.254.169.254`、`metadata.google.internal`、`100.100.100.200`。
- production/strict 模式默认拒绝 localhost、loopback、link-local、unique-local IPv6 和私网 IP，除非显式 allowlist。
- discovery 候选 endpoint 必须通过 URL safety；失败时 Tempo 标为 `rejected`，其他后端标为 `degraded`。
- production discovery 即使高置信度也只会到 `requires_review`，不会自动 ready。

runtime secret 通过 `SecretStr` 或 `RuntimeBackendAuthConfig` 只在 client 构造时解开。安全表示应该是 redacted metadata，例如 auth type 或 secret ref；raw token/DSN/password 不应进入 DB、audit、log、prompt、state 或文档样例。

## Discovery 与发布边界

Discovery 负责发现候选后端，不负责直接切换 worker：

DiscoveryRunner 阶段、service label detection、metric mapping、capability matrix、workload binding、service edge 和 manual/auto rerun 细节见 [Discovery、Capability Matrix 与服务拓扑技术深挖](discovery-capability-topology-deep-dive.md)。

```text
K8s services/endpoints/ingress
  -> BackendEndpointDetector
  -> BackendEndpoints(status=detected_only|requires_review|ready|degraded|rejected)
  -> proposal/review/publish
  -> EffectiveConfigVersion(published)
  -> worker _build_deps()
```

关键边界：

- manual URL 存在时，discovery 不覆盖该 backend。
- 低置信度结果是 `detected_only`。
- 多个候选结果是 `requires_review`。
- 生产环境结果最高也是 `requires_review`。
- `auth_required_unknown=true` 时需要 review。
- Tempo discovery 需要 M9 Tempo discovery 子能力开启。
- worker 只读取 published config 和 active override，不读取 proposal。

## 只读诊断与执行器边界

Observability adapters 负责收集证据，不负责执行修复。

| 路径 | 是否可写 | 说明 |
|------|----------|------|
| MetricsTool | 否 | Prometheus read API。 |
| LogsTool | 否 | Loki read API。 |
| TraceTool | 否 | fixture/Jaeger/Tempo read API。 |
| GitChangeTool | 否 | fixture/GitHub/Argo read API。 |
| K8sDiagnosticsTool | 否 | Kubernetes read-only diagnostics。 |
| DbDiagnosticsTool | 否 | 固定 SELECT，read-only connection。 |
| FixtureExecutorBackend | fixture 写模拟 | 默认执行器，用于 demo/test/CI。 |
| LiveK8sExecutorBackend | 受控 Kubernetes mutation | 仅 `EXECUTOR_BACKEND=live`，且必须经过 guardrail/approval。 |

不要通过扩展 diagnostics tool 添加真实写操作。新增真实 remediation 必须走 executor backend、capability metadata、guardrail、approval、snapshot、verify 和文档/测试门禁。

## 降级排查路径

当 UI 或报告里看到工具证据缺失，按下面顺序排查：

1. 看 `tool_calls.status`，区分 `degraded`、`timeout`、`failed`。
2. 看 `tool_calls.output_summary` 和 error，确认是 no data、backend unavailable、URL missing、timeout、read-only refused 还是 auth/RBAC。
3. 看 `cache_key` 和 `cache_hit`，判断是否命中同一 run 的缓存。
4. 看 `evidence_items` 是否存在；degraded/no data 通常不会写 evidence。
5. 对 Prometheus/Loki/trace，看 published `EffectiveConfigVersion` 和 active override。
6. 对 GitHub/Argo/K8s/DB/executor，看 settings/env/deploy manifest。
7. 对 production discovery，看 proposal 是否还停在 `requires_review`，是否尚未 publish。
8. 对 Tempo，看 `M9_EXTENSIONS_ENABLED` 和 Tempo 子开关是否开启。
9. 对 K8s live diagnostics，看 ServiceAccount 是否具备 read-only RBAC。
10. 对 DB live diagnostics，看 DSN、statement timeout、`pg_stat_statements` 和 read-only query 是否可用。

## 常见误区

| 误区 | 正确口径 |
|------|----------|
| discovery 找到 endpoint 后 worker 会自动使用 | 不会。生产必须 review/publish，worker 读取 published config。 |
| 一个 Agent 可以同时接多个 Prometheus | 当前不支持。同一实例只有一个 active Prometheus URL。 |
| K8s live diagnostics 可以执行重启 | 不可以。diagnostics 只读，重启属于 executor。 |
| DB diagnostics 可以执行任意 SQL | 不可以。只允许预定义 SELECT，且连接 read-only。 |
| `degraded` 是 run 失败 | 不是。它是可解释降级，Agent 会继续使用其他证据。 |
| 没有 evidence 就没有工具调用 | 不一定。degraded/unavailable/timeout 仍会留下 `tool_calls`。 |
| M9 关闭会禁用 Jaeger | 不会。Jaeger 是 M8 已验证 trace backend。M9 关闭会降级 Tempo。 |
| GitChangeTool 只读 Git commit | 当前语义是 deployment change，支持 fixture/GitHub/Argo CD 只读来源。 |

## 新增或修改后端适配器 Checklist

修改 observability adapter 时，按这个清单收口：

- 保持 `BaseTool.run(query)` 或 backend protocol 可单元测试。
- 定义明确 Pydantic query/result shape，避免 ad-hoc dict 扩散。
- 所有外部调用有 timeout。
- 无数据、HTTP 错误、解析错误优先返回 `degraded` 或 `timeout`，不要静默吞异常。
- summary 和 error 不包含 secret、DSN、token、raw authorization header。
- 如果新增 cache，使用 UTC bucket 和 normalized query hash。
- 如果同一工具支持多个 datasource，把 datasource 纳入 cache hash。
- live diagnostics 必须只读；真实 mutation 只能走 executor backend。
- production URL 必须经过 URL safety、review/publish 或明确 allowlist 路径。
- M9 能力必须受 global gate 和子开关控制，默认生产关闭。
- 更新 `tool-layer.md`、`configuration.md`、`backend-connectivity.md` 和本文。
- 增加 mocked backend/degraded/read-only/secret leakage 测试说明。

## 测试定位

不要只用端到端 demo 验证后端适配器。当前更可靠的测试分层是：

| 行为 | 测试入口 |
|------|----------|
| Metrics/Logs/Trace/Git 工具与 cache | `tests/unit/test_tools_phase2.py` |
| K8s diagnostics read-only 和 live backend safety | `tests/unit/test_k8s_diagnostics_tool.py`、`tests/unit/test_live_k8s_diagnostics.py` |
| DB diagnostics read-only | `tests/unit/test_db_diagnostics_tool.py` |
| URL safety | `tests/unit/test_backend_url_safety.py` |
| EffectiveConfig merge | `tests/unit/test_config_merge.py` |
| Worker 使用 published EffectiveConfig | `tests/integration/test_worker_with_effective_config.py` |
| Discovery endpoint status | `tests/unit/test_backend_endpoint_detector.py` |
| Tool calls/evidence persistence | `tests/integration/test_tool_call_persistence.py` |

本仓库的当前约束是由用户本地运行测试；Codex 更新文档时只做静态检查，并给出建议命令。
