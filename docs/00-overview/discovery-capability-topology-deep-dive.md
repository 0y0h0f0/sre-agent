# Discovery、Capability Matrix 与服务拓扑技术深挖

最后更新：2026-06-18

本文说明当前项目的 discovery 如何发现服务、后端 endpoint、metric mapping、workload binding、service edges 和 capability matrix。

它补充：

- [配置、Discovery 与 EffectiveConfig 技术深挖](config-discovery-effective-config-deep-dive.md)
- [Observability 与后端适配器技术深挖](observability-backend-adapters-deep-dive.md)
- [K8s 后端对接验证](../08-deploy/k8s-backend-verification.md)
- [后端对接范围](../11-reference/backend-connectivity.md)

本文重点不是配置发布流程。配置 publish/rollback/override 仍以 `ConfigPublisher`、Config API 和 EffectiveConfig 文档为准。本文聚焦 discovery run 自身：

- 它从哪些后端读取发现信息。
- 哪些失败会降级而不是崩溃。
- API 读端点如何从 `DiscoveryRun.summary` 重建展示。
- capability matrix 当前如何生成，`CapabilityAssessor` 当前为何只是独立 helper。
- service topology 当前能推导什么，不能推导什么。
- manual/auto discovery 为什么不会自动改变 worker runtime config。

## 一句话模型

```text
manual API / Beat / worker startup
  -> DiscoveryRun(status=running)
  -> DiscoveryRunner
       -> K8s discovery
       -> backend endpoint detection
       -> Prometheus metric discovery
       -> Loki label discovery
       -> Jaeger service discovery
       -> service list
       -> topology
       -> capability matrix
  -> DiscoveryRun.summary
  -> optional DiscoveryProposal(status=pending_review)

Discovery read API
  -> recent DiscoveryRun.summary
  -> fallback latest published config for capabilities only
  -> UI/API display

Worker diagnosis
  -> reads published EffectiveConfig + overrides
  -> never reads pending DiscoveryProposal directly
```

核心边界：

- Discovery 是发现和建议，不是运行时配置切换。
- `DiscoveryRun.summary` 是展示/审计数据，不是 worker source of truth。
- `DiscoveryProposal(status=pending_review)` 不会自动进入 worker。
- `detected_only`、`requires_review`、`degraded` endpoint 都不能绕过 config publish。
- 生产环境 discovery 默认关闭；启用后也不会自动发布 backend URL。

## 代码入口

| 能力 | 当前入口 | 说明 |
|------|----------|------|
| Discovery read API | `apps/api/routers/discovery.py` | `/status`、`/services`、`/metrics`、`/topology`、`/capabilities`。 |
| Manual rerun API | `apps/api/routers/discovery.py` `trigger_discovery_rerun()` | 创建 run、拿 Redis TTL lock、入队 worker task。 |
| Worker rerun task | `apps/worker/tasks.py` `run_discovery_rerun()` | 执行 runner、落库 summary、创建 pending proposal。 |
| Auto discovery task | `apps/worker/tasks.py` `auto_discovery_rerun()` | Beat/startup 触发，满足开关和 live K8s 后运行。 |
| Runner 编排 | `packages/discovery/runner.py` | 串起 K8s、backend、Prometheus、Loki、Jaeger、topology、capability matrix。 |
| K8s discovery | `packages/discovery/k8s_discovery.py` | 读取 namespaces、services、pods、workloads、endpoints、ingresses、configmaps。 |
| Backend endpoint detection | `packages/discovery/backend_endpoints.py` | 从 K8s service/ingress 推断 Prometheus/Loki/Jaeger/Alertmanager/Tempo endpoint。 |
| Prometheus discovery | `packages/discovery/prom_discovery.py`、`metric_matcher.py` | list metrics、detect service label、匹配语义 metric。 |
| Loki discovery | `packages/discovery/loki_discovery.py` | detect log service label。 |
| Jaeger discovery | `packages/discovery/jaeger_discovery.py` | 当前只发现 service list，不聚合 call graph。 |
| Topology | `packages/discovery/topology.py` | workload binding 和 service edge 推导。 |
| Capability helper | `packages/discovery/capability_assessor.py` | 独立 deterministic degradation report helper；当前未自动接入 API/runner。 |
| Persistence | `packages/discovery/store.py` | 写 `DiscoveryRun.summary` 和 `DiscoveryProposal`。 |
| Proposal helper | `packages/discovery/config_proposal.py`、`automation_policy.py` | 结构化 proposal/automation helper；当前 rerun task 没用它自动 publish。 |

## 关键数据对象

| 对象 | 位置 | 当前用途 |
|------|------|----------|
| `DiscoveryRun` | `discovery_runs` | 一次发现运行，记录 source/status/trigger/summary。 |
| `DiscoveryProposal` | `discovery_proposals` | 发现结果转成配置建议，当前 manual rerun 只创建 `pending_review`。 |
| `EffectiveConfigVersion` | `effective_config_versions` | 已发布 runtime config；worker 只读取 latest `published`。 |
| `DiscoveryResult` | `packages/discovery/models.py` | runner 输出的统一模型。 |
| `MetricMapping` | `packages/discovery/models.py` | 语义 metric 到 Prometheus metric/PromQL template 的映射。 |
| `CapabilityMatrix` | `packages/discovery/models.py` | service 级诊断能力矩阵。 |
| `WorkloadBindingModel` | `packages/discovery/models.py` | K8s Service 到 Workload 的绑定。 |
| `ServiceEdgeModel` | `packages/discovery/models.py` | 推导出的服务依赖边。 |
| `BackendEndpoint` | `packages/discovery/models.py` | 发现到的 observability backend URL 候选。 |

`DiscoveryRun.summary` 当前保存：

- `total_services_discovered`
- `total_metrics_scanned`
- `duration_seconds`
- `warnings`
- `degraded_signals`
- `backend_count`
- `services`
- `metric_mappings`
- `backend_endpoints`
- `capability_matrix`
- `workload_bindings`
- `service_edges`

这些字段是 Discovery read API 的主要数据来源。

## API 读端点

读端点需要：

```text
discovery:read 或 discovery:write
```

| Endpoint | 数据来源 | 行为 |
|----------|----------|------|
| `GET /api/discovery/status` | 最近 `DiscoveryRun` | 返回 `discovery_enabled`、latest run 和 recent runs。 |
| `GET /api/discovery/services` | 最近 20 个 run 中第一个带 `summary.services` 的 run | 返回服务名、namespace、labels、sources。 |
| `GET /api/discovery/metrics` | 最近 20 个 run 中第一个带 `summary.metric_mappings` 的 run | 返回 semantic metric mappings。 |
| `GET /api/discovery/topology` | 最近 20 个 run 中第一个带 topology 数据的 run | 返回 workload bindings 和 service edges。 |
| `GET /api/discovery/capabilities` | 最近 20 个 run 中第一个带 `summary.capability_matrix` 的 run；否则 latest published config 的 `capabilities` | 返回服务级能力矩阵。 |

注意：

- `/services`、`/metrics`、`/topology`、`/capabilities` 不实时访问 Prometheus/Loki/K8s/Jaeger。
- 它们从已落库 summary 重建展示。
- 它们不要求最新 run 必须是 `succeeded`；代码按最近 run 顺序寻找有对应数据的 summary。
- `/capabilities` 是唯一会在 run summary 没有数据时读取 latest published config snapshot 的 discovery read endpoint。

## Manual Rerun

API：

```text
POST /api/discovery/rerun
```

权限：

```text
discovery:write
```

当前流程：

1. API 创建 `DiscoveryRun(source="manual_rerun", trigger_type="manual")`。
2. 尝试连接 Redis。
3. Redis 可用时尝试获取 `RedisLock("discovery:runner", ttl=300)`。
4. 锁已存在时返回 HTTP 202，`status="locked"`，不入队。
5. Redis 不可用时继续入队，不使用 lock。
6. 调用 `enqueue_discovery_rerun_task(discovery_run_id)`。
7. 写 audit：`discovery.rerun_requested`。
8. commit。

当前实现差异：

- `DISCOVERY_MANUAL_RERUN_ENABLED` 是 settings 字段，但当前 router 没有显式检查它。
- manual rerun 的实际控制是 API key auth、`discovery:write` scope、Redis lock 和 Celery enqueue。
- API 成功入队后没有把 `discovery:runner` lock 交给 worker 释放；当前依赖 300 秒 TTL 自然过期。入队失败时 API 会尝试释放 lock。
- locked 响应不代表已有 run 已经成功或失败，只表示并发 rerun 被挡住。

## Worker Rerun Task

任务：

```text
apps.worker.tasks.run_discovery_rerun(discovery_run_id, triggered_by=None)
```

流程：

1. 打开 DB session。
2. 用 `DiscoveryStore.get_run(discovery_run_id)` 读取 run。
3. 找不到 run 时抛 `NotFoundError`。
4. 调用 `_build_discovery_runner(settings)`。
5. 执行 `runner.run(run_id=discovery_run_id)`。
6. `store.finish_run(run, result, status=result.status)` 写 summary。
7. 如果有 `result.backend_endpoints` 或 `result.metric_mappings`，创建 `DiscoveryProposal(status="pending_review")`。
8. proposal 的 `config_diff` 来自 `_result_to_config_diff(result)`。
9. confidence：`succeeded` 为 `0.8`，其它状态为 `0.5`。
10. 写 audit：`discovery.rerun_complete`。
11. commit。

失败路径：

- task 异常时重新打开 session。
- 如果 run 存在，用空 `DiscoveryResult(status="failed")` 标记 failed。
- 记录 `error_message`。

当前 worker rerun 不做：

- 不调用 `ConfigPublisher.publish()`。
- 不读取或应用 `ConfigProposalGenerator.ready_to_publish`。
- 不自动把 proposal 变成 `EffectiveConfigVersion`。
- 不把 `detected_only` endpoint 直接写入 worker runtime config。

## Auto Discovery

任务：

```text
apps.worker.tasks.auto_discovery_rerun()
```

触发：

- Celery Beat 每 30 分钟调度。
- worker finalize 后也会尝试入队一次 startup discovery task。

真正运行前会检查：

```text
DISCOVERY_ENABLED=true
K8S_BACKEND=live
```

并尝试 Redis lock：

```text
RedisLock("lock:discovery:auto", ttl=60)
```

当前 auto discovery 行为：

1. 不满足开关或 live K8s 条件时返回 skipped。
2. 锁已存在时返回 `discovery_lock_held`。
3. 创建 `DiscoveryRun(source="auto_periodic", trigger_type="periodic")`。
4. 运行 runner。
5. 写 summary。
6. 写 audit：`discovery.auto_complete`。
7. commit。

当前 auto discovery 不创建 `DiscoveryProposal`，也不 publish config。

生产环境未显式设置时，`Settings` 会把 `DISCOVERY_ENABLED` 改为 `false`。因此生产默认不会因为 Beat/startup 自动扫描集群。

## Runner 阶段

`DiscoveryRunner.run()` 当前按固定顺序执行：

```text
K8s discovery
  -> backend endpoint detection
  -> Prometheus discovery
  -> Loki discovery
  -> Jaeger service discovery
  -> service list merge
  -> topology derivation
  -> capability matrix build
```

各阶段互相独立降级：

- 单个后端异常会写 `warnings` 和 `degraded_signals`。
- runner 不会因为 Prometheus/Loki/Jaeger/K8s 某个后端不可用而整体崩溃。
- 只有没有 discovered services 且没有 metric mappings 时，runner status 才是 `failed`。
- 只要有服务或 metric mapping，但有 degraded signals，status 是 `degraded`。
- 没有 degraded signals 时 status 是 `succeeded`。

## K8s Discovery

`K8sDiscovery` 读取：

- namespaces
- services
- pods
- deployments
- statefulsets
- daemonsets
- endpoints
- ingresses
- configmaps

启用条件：

- `K8S_BACKEND=live`，或显式传入 kube config file。
- `K8S_NAMESPACE` 可作为 namespace allowlist，支持逗号分隔。

降级行为：

- Kubernetes Python package 缺失会被缓存 30 秒，避免反复 import。
- 不在 live K8s 模式时，discovery 报 `Kubernetes discovery is disabled unless K8S_BACKEND=live`。
- namespace list 因 RBAC forbidden 失败时，如果配置了 namespace allowlist，会退回 allowlist。
- 单个 namespace 的 services/pods/workloads/endpoints/ingresses/configmaps 读取失败，只记录 warning，不中断其它 namespace。
- 如果 services 和 workloads 都为空，结果标记 degraded。

K8s discovery 还会提取：

- workload env var 里的 `*.svc` 服务引用。
- workload 引用的 ConfigMap 名称。
- ConfigMap 内容里的 `*.svc` 服务引用，但只保留 service refs，不保存原始 ConfigMap 值。

这避免把完整配置内容当作 topology evidence 写入 summary。

## Backend Endpoint Detection

`BackendEndpointDetector` 从 K8s services/endpoints/ingresses 推断 observability backend：

- Prometheus：`prometheus`、`prom`、`thanos`，默认端口 9090。
- Loki：`loki`、`loki-distributed`，默认端口 3100。
- Jaeger：`jaeger`、`jaeger-query`，默认端口 16686。
- Alertmanager：`alertmanager`、`alertmanager-operated`，默认端口 9093。
- Tempo：`tempo`、`tempo-query`、`tempo-distributed`、`tempo-distributor`，默认端口 3200。

规则：

- 已显式配置的 manual URL 不会被 discovery 覆盖。
- Tempo discovery 需要 M9 `tempo_discovery` 子能力开启。
- 优先从 Service DNS 构造 URL，例如 `http://prometheus.monitoring.svc.cluster.local:9090`。
- 如果 service port 匹配预期端口或端口名，confidence 更高。
- endpoint 有 addresses 时 confidence 更高。
- ingress-only candidate confidence 约为 `0.75`。
- 多 candidate 会进入 evidence 的 `candidates` 列表。

Endpoint status：

| Status | 当前含义 |
|--------|----------|
| `ready` | 非生产、单候选、confidence > 0.70、auth 已知。当前普通 K8s 推断默认 `auth_required_unknown=True`，所以多数不会 ready。 |
| `requires_review` | 多候选、生产环境、auth unknown 或其它需要人工 review 的候选。 |
| `detected_only` | confidence <= 0.70。 |
| `degraded` | URL safety failed，普通 backend 使用 degraded。 |
| `rejected` | Tempo URL safety failed 时使用 rejected。 |
| `unavailable` | 未找到候选；生产环境不可用，非生产通常 degraded。 |

URL safety 失败不会被忽略：

- 普通 backend 标记 `degraded`。
- Tempo 标记 `rejected`。
- 发现结果仍只能作为 proposal/review evidence，不能直接进入 worker。

## Prometheus Discovery

Prometheus discovery 包含两层：

1. service label detection。
2. semantic metric matching。

### Service label detection

候选 label：

- `service`
- `app`
- `job`
- `container`
- `deployment`
- `statefulset`
- `daemonset`
- `app_kubernetes_io_name`

算法：

- 最多采样 200 个 metric names。
- 对每个 metric 调 `/api/v1/series`。
- 统计候选 label 在 series labels 中出现的覆盖率。
- 覆盖率 >= 80% 才使用 detected label。
- 否则使用 settings 中的 `metrics_service_label` 默认值，并写 warning。

### Metric matching

`MetricMatcher` 针对五个 semantic types 做匹配：

| Semantic type | 核心程度 | 示例候选 |
|---------------|----------|----------|
| `latency` | core | HTTP/gRPC duration histogram/summary。 |
| `error_rate` | core | HTTP request count with status/code，或 error counter。 |
| `qps` | core | HTTP request total/count 或 requests per second gauge。 |
| `cpu_throttle` | extended | `container_cpu_cfs_throttled_seconds_total`。 |
| `disk_avail` | extended | `node_filesystem_avail_bytes`。 |

匹配逻辑：

- 按 candidate priority 排序。
- 先用 regex 匹配 metric name。
- 再校验 required labels。
- 再校验 metadata type。
- 返回第一个可用 mapping。
- 找不到时返回 `status="unavailable"` 和 degraded reason。

当前 runner 使用 `MetricMatcher.match()` 生成 `MetricMapping`。`PromQLBuilder` 和 `PromQLValidator` 是已有 helper，但当前 runner 没有对每个 mapping 做三窗口 dry-run validation。

## Loki Discovery

Loki discovery 当前用于识别 log service label。

候选 label 与 Prometheus 类似：

- `service`
- `app`
- `job`
- `container`
- `deployment`
- `statefulset`
- `daemonset`
- `app_kubernetes_io_name`

算法：

1. 调 `/loki/api/v1/labels` 获取 label keys。
2. 只保留存在于 Loki 的候选 label。
3. 对每个候选 label 获取 label values。
4. 每个 label 最多采样 20 个 value。
5. 对每个 value 发 `query_range`，检查是否有 stream。
6. coverage >= 80% 才选用 detected label。
7. 否则使用 settings 中的 `logs_service_label` 默认值，并写 warning。

当前 capability matrix 的 `logs_available` 逻辑比较宽松：只要 runner 能走到 label 检测路径，默认 label 也会让 logs capability 表现为可用。排查时仍应结合 runner warnings 和真实 `LogsTool` tool calls 判断。

## Jaeger Discovery

当前 `JaegerDiscoveryClient` 只调用：

```text
GET /api/services
```

它返回：

- `available_services`
- `status`
- `confidence`
- `degraded_reason`

当前不会：

- 读取 traces。
- 聚合调用图。
- 生成 trace-based service edges。

`TopologyDeriver` 接受 `trace_edges` 参数，但 runner 当前只传 `trace_services`。代码注释明确：bare service list 只能证明 trace availability，不能单独创建 `ServiceEdge`。

## Service List 合并

runner 的 service list 来自：

- K8s workloads：source `k8s_workload`。
- K8s services：source `k8s_service`。
- Jaeger services：source `jaeger_trace`。

同名 service 会合并 sources。

注意：

- service name 以当前数据源里的名称为准。
- K8s service 和 workload 同名时会合并。
- Jaeger-only service 没有 namespace 和 labels。
- 当前没有复杂的跨 namespace 同名 service disambiguation；读者应结合 namespace、labels 和 workload binding 判断。

## Workload Binding

`derive_workload_bindings()` 只做 K8s Service 到 Workload 的绑定，不创建 service edge。

推导路径：

```text
Service.selector
  -> matching Pods by labels
  -> Pod.owner_references
  -> Deployment / StatefulSet / DaemonSet
```

特殊处理：

- Pod owner 是 ReplicaSet 时，会用 ReplicaSet 名称前缀推回 Deployment。
- 找不到 owner ref 或匹配 workload 时，不产生 binding。
- 同一个 service/workload/kind/namespace 只保留一个 binding。

输出 evidence 包括：

- service selector
- sample pod
- pod labels
- owner references

## Service Edge

`derive_service_edges()` 支持四类策略：

| Strategy | 默认 confidence | 来源 |
|----------|------------------|------|
| `manual` | 1.0 | 显式传入的手工 topology。 |
| `trace` | 至少 0.85 | 显式 trace call graph edge。 |
| `env` | 0.6 | workload env var 中的 `*.svc` 引用。 |
| `configmap` | 0.5 | workload 引用的 ConfigMap 内容里的 `*.svc` 引用。 |

冲突处理：

- key 是 `(source_service, target_service)`。
- 同一边出现多种证据时，confidence 更高的 edge 覆盖低 confidence edge。
- 输出按 confidence 降序排序。

当前 runner 的实际 edge 来源：

- K8s workload env var service refs。
- K8s ConfigMap service refs。
- 传入 `trace_services` 只用于兼容，不创建 edge。
- 当前没有从 Jaeger trace API 自动构造 trace edge。
- 当前没有从 `demo/topology.json` 自动导入 manual edge 到 discovery runner。

因此，`/api/discovery/topology` 展示的是 discovery 推导 topology，不等于 Agent 诊断里的完整服务依赖图。Agent 级联故障分析还会读取 demo topology 或其它 graph context。

## Capability Matrix

runner 的 `_build_capability_matrix()` 为每个 service 生成：

- `metrics_available`
- `logs_available`
- `traces_available`
- `k8s_accessible`
- `metric_mappings`
- `capability_gaps`

当前规则：

- `metrics_available`：存在任一 available metric mapping，并且 service labels 中包含 detected Prometheus label，或 detected label 不是默认 `"service"`。
- `logs_available`：当前逻辑宽松，默认 service label 也会让它为 true。
- `traces_available`：service name 出现在 Jaeger service list。
- `k8s_accessible`：service name 出现在 K8s Service 列表。
- capability gaps 根据上述 boolean 生成：`metrics_unavailable`、`logs_unavailable`、`traces_unavailable`、`k8s_inaccessible`。
- 每个 service 当前会携带同一组 `metric_mappings`，不是逐 service 验证后的映射。

这意味着 capability matrix 是发现层面的能力概览，不是一次 incident 的最终证据质量判定。最终诊断仍应看对应 Agent run 的 `tool_calls`、`evidence_items` 和 node trace。

## CapabilityAssessor 的真实位置

`packages/discovery/capability_assessor.py` 提供独立 helper：

```text
DiscoveryResult -> DegradationReport
```

它会计算：

- global capability gaps。
- degraded signals。
- fallback signals。
- confidence adjustment。
- per-service gaps。
- overall：`healthy`、`degraded`、`critical`。

核心 metric：

- core：`latency`、`error_rate`、`qps`。
- extended：`cpu_throttle`、`disk_avail`。

当前实现差异：

- `CapabilityAssessor` 有单元测试。
- 但当前 `DiscoveryRunner.run()` 没有调用它。
- Discovery read API 也没有调用它。
- `/api/discovery/capabilities` 返回的是 runner 写入 summary 的 `capability_matrix`，不是 `DegradationReport`。

如果后续要把 `CapabilityAssessor` 接入 API 或 worker，应同步更新本文、API schema、测试和 UI 文案。

## DiscoveryProposal 边界

manual rerun worker task 当前在这些条件下创建 proposal：

```text
result.backend_endpoints or result.metric_mappings
```

proposal 内容：

- `proposal_id`：`dp_`。
- `discovery_run_id`：关联 run。
- `status = pending_review`。
- `config_diff.backend_endpoints`：backend type、url、status。
- `config_diff.metric_mappings`：semantic type、metric name、status。
- `confidence = 0.8` if result succeeded else `0.5`。

当前不会：

- 把 proposal 自动 publish。
- 把 proposal 标记 auto_applied。
- 自动更新 `EffectiveConfigVersion`。
- 自动改写 `PROMETHEUS_URL`、`LOKI_URL` 等 settings。

`ConfigProposalGenerator` 和 `AutomationPolicy` 是已有 helper：

- 可以生成结构化 `ConfigProposal` 和 item decisions。
- production backend URL 永远 requires review。
- executor config 永远 rejected。
- confidence 阈值由 automation level / apply mode 决定。

但当前 `run_discovery_rerun()` 不是用它们生成/publish proposal，而是用 `_result_to_config_diff()` 的简化 diff。

## 与 EffectiveConfig 的分界

Worker runtime config 来自：

```text
settings/env + active overrides + latest published EffectiveConfigVersion + safe defaults
```

不来自：

- `DiscoveryRun.summary`
- `DiscoveryProposal(status=pending_review)`
- backend endpoint `requires_review`
- backend endpoint `detected_only`
- auto discovery result
- capability matrix
- topology result

因此，“discovery 找到了 Prometheus/Loki”只说明候选已被记录。要让 production worker 使用它，需要 operator 通过 Config API publish 受审配置。

## 与 Agent 诊断的关系

Discovery 影响 Agent 的方式是间接的：

- 已发布 EffectiveConfig 影响 worker `_build_deps()` 构造 metrics/logs/trace tools。
- service label 配置影响 metrics/logs 查询。
- K8s discovery/topology 可以帮助 operator 判断后端对接和服务识别。
- capability matrix 可以提示某 service 缺少 metrics/logs/traces/k8s 访问。

Discovery 不会直接：

- 修改 Agent state。
- 修改当前运行中的 checkpoint。
- 自动重跑 incident。
- 自动执行 remediation。
- 自动放宽 guardrail。
- 自动启用 live executor。

## 安全边界

Discovery 遵守这些边界：

- 生产默认 `DISCOVERY_ENABLED=false`。
- 生产 backend URL candidate 最多 `requires_review`。
- backend URL 必须通过 URL safety。
- manual URL 不被 discovery 覆盖。
- `DISCOVERY_MANUAL_RERUN_ENABLED` 当前不是 router enforce 开关；不要把它当作安全控制。
- `discovery:write` 是 manual rerun 的主要权限控制。
- discovery proposal 不等于 published config。
- auto discovery 不创建 proposal，不 publish config。
- Tempo discovery 受 M9 gate 控制。
- 不写 raw secret 到 discovery summary。
- K8s ConfigMap 只提取 service refs，不保存原始值。

## 调试清单

### `/api/discovery/status` 没有 run

检查：

1. worker 是否启动。
2. Celery Beat 是否运行。
3. `DISCOVERY_ENABLED` 是否开启。
4. `K8S_BACKEND` 是否为 `live`，auto discovery 需要它。
5. manual rerun 是否返回 `enqueued`。
6. worker task 是否失败。

### Manual rerun 返回 locked

检查：

1. Redis 中 manual discovery lock 是否仍在 TTL 内。
2. 上一次 rerun 是否刚刚入队。
3. 当前实现成功入队后依赖 TTL 过期，不由 worker 显式释放。
4. 如果 Redis 不可用，当前 API 会继续入队而不是 locked。

### Discovery succeeded 但 worker 仍读不到后端

这是预期边界。检查：

1. 是否创建了 `DiscoveryProposal`。
2. proposal 是否仍是 `pending_review`。
3. 是否调用 Config API publish。
4. `effective_config_versions` 是否有 latest `published`。
5. worker `_build_deps()` 是否记录了 `config_version_id`。

### `/api/discovery/services` 为空

检查：

1. 最近 20 个 discovery run 是否有 `summary.services`。
2. K8s discovery 是否 degraded。
3. `K8S_NAMESPACE` allowlist 是否排除了目标 namespace。
4. Jaeger discovery 是否只有 unavailable/degraded。
5. 服务是否只存在于 alert labels，还没有被 discovery 数据源看到。

### `/api/discovery/topology` 没有边

检查：

1. K8s workloads 是否有 env var `*.svc` 引用。
2. workloads 是否引用 ConfigMap。
3. ConfigMap 内容是否包含 `*.svc` DNS。
4. 不要期待 bare Jaeger service list 自动生成 call graph edge。
5. workload binding 和 service edge 是两种不同对象；有 binding 不代表有 edge。

### `/api/discovery/capabilities` 看起来过于乐观

检查：

1. 当前 `logs_available` 逻辑较宽松。
2. 每个 service 共享同一组 metric mappings。
3. 这只是 discovery summary，不是 incident-level evidence。
4. 真实可用性应结合 Agent run 的 `tool_calls`。

### Tempo 没被发现

检查：

1. `M9_EXTENSIONS_ENABLED=true`。
2. `TEMPO_DISCOVERY_ENABLED=true`。
3. K8s Service 名称、label 或 annotation 是否匹配 tempo pattern。
4. URL safety 是否拒绝候选。
5. 生产环境即使发现也应是 `requires_review`，不会自动 published。

## 当前实现差异与不要误读

- `DISCOVERY_MANUAL_RERUN_ENABLED` 当前没有在 router 中 enforce。
- `CapabilityAssessor` 当前没有接入 runner/API。
- `PromQLValidator` 当前没有接入 runner 的 metric mapping 验证路径。
- `ConfigProposalGenerator` 和 `AutomationPolicy` 当前没有接入 `run_discovery_rerun()` 的 publish 路径。
- manual rerun lock 当前成功入队后依赖 300 秒 TTL。
- auto discovery 不创建 `DiscoveryProposal`。
- Jaeger discovery 只列服务，不生成 trace call graph。
- `trace_services` 不会创建 topology edge。
- discovery topology 不等于 Agent 级联故障图。
- capability matrix 不等于工具调用成功率。
- discovery summary 不等于 runtime config。

## 测试入口

根据项目测试策略，Codex 不直接运行测试。修改 discovery/capability/topology 相关代码后，建议由用户本地运行：

```bash
pytest tests/unit/test_discovery_runner.py tests/unit/test_discovery_store.py tests/unit/test_discovery_models.py -v
pytest tests/unit/test_k8s_discovery.py tests/unit/test_discovery_topology.py tests/unit/test_topology.py -v
pytest tests/unit/test_capability_assessor.py tests/unit/test_discovery_cost_control.py -v
pytest tests/unit/test_config_proposal.py tests/unit/test_config_publisher.py tests/unit/test_config_merge.py -v
pytest tests/integration/test_discovery_api.py tests/integration/test_discovery_api_auth.py tests/integration/test_discovery_rerun_api.py -v
pytest tests/integration/test_worker_with_effective_config.py tests/integration/test_config_api.py -v
```

如果变更涉及 M9 Tempo discovery，还应补充：

```bash
pytest tests/unit/test_tempo_endpoint_detection.py tests/e2e/test_m9_tempo_grafana.py -v
```

## 文档维护规则

Discovery/capability/topology 行为变化时，至少同步：

- 本文。
- [配置、Discovery 与 EffectiveConfig 技术深挖](config-discovery-effective-config-deep-dive.md)。
- [API 参考](../01-backend/api-reference.md)。
- [数据模型](../01-backend/data-model.md)，如果表/字段/状态变化。
- [配置参考](../11-reference/configuration.md)，如果开关、默认值或 rollout 行为变化。
- [K8s 后端对接验证](../08-deploy/k8s-backend-verification.md)，如果验证路径变化。
- [后端对接范围](../11-reference/backend-connectivity.md)，如果多 namespace、多后端或服务识别边界变化。

尤其不要把以下内容写成当前能力：

- discovery 自动发布 production backend URL。
- pending proposal 自动进入 worker。
- Tempo discovery 绕过 M9 gate。
- Jaeger service list 自动生成调用图。
- capability matrix 等于工具调用成功率。
- 不要说关闭 `DISCOVERY_MANUAL_RERUN_ENABLED` 会拦截当前 rerun API。
