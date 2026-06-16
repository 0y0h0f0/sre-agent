# Real Backend Integration Design

**Date:** 2026-06-10
**Status:** draft
**Scope:** agentp 接入真实 Prometheus + Loki + Jaeger + Kubernetes 后端，并实现插即用适配；生产模式下以只读、可审计、可回滚的自动化为边界

---

## 1. 背景与目标

### 1.1 当前状态

agentp 已完成本地 demo 范围内的全部功能。工具层已通过 Backend Protocol 抽象为每种数据源提供了 `fixture` 和真实后端的切换能力：

| 工具 | Fixture | 真实后端 | 切换方式 |
|------|---------|---------|---------|
| MetricsTool | — | Prometheus HTTP API | 配置 `PROMETHEUS_URL`，天然支持 |
| LogsTool | — | Loki HTTP API | 配置 `LOKI_URL`，天然支持 |
| TraceTool | `FixtureTraceBackend` | `JaegerTraceBackend` | `TRACE_BACKEND=jaeger` |
| GitChangeTool | `FixtureDeploymentBackend` | `GitHubDeploymentBackend` / `ArgoCDDeploymentBackend` | `DEPLOYMENT_BACKEND=github/argocd` |
| K8sDiagnosticsTool | `FixtureK8sBackend` | `LiveK8sBackend` | `K8S_BACKEND=live` |
| DbDiagnosticsTool | `FixtureDbBackend` | `LiveDbBackend` | `DB_DIAGNOSTICS_BACKEND=live` |
| LLM | `FakeLLM`（CI/tests） | 生产默认 `disabled`（纯确定性诊断）；可选 `DeepSeekAdapter` / `OpenAIAdapter` / `AnthropicAdapter`（云端）或 `vLLM` 兼容 API（本地） | 生产默认 `LLM_PROVIDER=disabled`；operator 可选择 `deepseek/openai/anthropic/vllm`；CI 固定 `fake` |

**问题**：虽然代码支持真实后端，但每次接入新环境需要手动适配标签约定、指标命名和拓扑关系。例如 Prometheus 中区分服务的 label 可能是 `app`、`service`、`job`、`app.kubernetes.io/name` 等，指标名也因采集方案（Istio、kube-prometheus-stack、OpenTelemetry）而异。

### 1.2 目标

将 agentp 打造为**对满足 Prometheus + Loki + Jaeger + K8s 标准栈的后端项目可快速适配**的系统。本地 demo / 非生产试用可以只配置 Prometheus、Loki、Jaeger、Alertmanager 等少量 URL；生产接入必须显式确认 LLM 策略：保持 `LLM_PROVIDER=disabled`（生产默认），或由 operator 选择 `deepseek/openai/anthropic/vllm` profile。不能把”4 个 URL”理解为生产最小安全配置。

生产安全模式下，目标进一步限定为：

- 后端项目可以零改动接入；agent 不要求业务服务新增指标、改 label、改日志格式或改部署配置。
- 如果后端项目没有暴露某类语义指标，agent 不伪造该指标证据；对应诊断能力降级为 `unavailable/degraded`，并转向日志、trace、K8s、部署、DB read-only、Runbook 和历史记忆等仍可读取的信号。
- 自动化遵循 `discover -> validate -> decide -> publish`：只有通过确定性校验、低风险、高置信、可回滚的结果才能自动发布；其他结果进入 review queue 或仅记录。
- 生产自动化优先减少人工配置和人工 triage，不自动放宽执行权限，不让 LLM、网页内容或 discovery 结果绕过 guardrail/approval。

### 1.3 非目标

- 不改变现有 Backend Protocol 抽象
- 不新增真实写入能力；默认 executor 仍为 fixture/mock，已有 `EXECUTOR_BACKEND=live` 只能由 operator 显式 opt-in，且继续受限于现有 Kubernetes 窄范围动作、guardrail、审批和二次确认
- 不改变 CI 稳定性要求（fixture 默认保持确定性测试）
- 不支持非 K8s 环境的自动发现（可通过 profile 手动适配）
- 不要求被接入的后端项目新增指标或修改代码
- 不在指标缺失时用 LLM 推断值、合成假证据或把低置信 discovery 结果直接用于生产诊断

---

## 2. 总体架构

```
                        ┌──────────────┐
                        │ Alertmanager  │  (+ Grafana 后续扩展)
                        └──────┬───────┘
                               │ webhook POST /api/alerts 或 agentp poll GET /api/v2/alerts
                               ▼
┌──────────────────────────────────────────────────────────────┐
│                       agentp                                 │
│                                                              │
│  ┌────────────────────┐   ┌──────────────────────────────┐   │
│  │  API (FastAPI)      │   │  Worker (Celery)             │   │
│  │  - POST /alerts     │   │                              │   │
│  │  - GET /discovery/* │   │  异步/周期/手动:               │   │
│  │  - GET /runbooks/*  │   │    DiscoveryRunner.run()      │   │
│  │  - POST /approval   │   │       │                       │   │
│  └────────┬───────────┘   │       ▼                       │   │
│           │               │  ┌─────────────────────────┐  │   │
│           │               │  │ Discovery Layer (新增)   │  │   │
│           │               │  │                         │  │   │
│           │               │  │ PromDiscovery           │  │   │
│           │               │  │ K8sDiscovery            │  │   │
│           │               │  │ LokiDiscovery           │  │   │
│           │               │  │ MetricMatcher           │  │   │
│           │               │  │ TopologyDeriver         │  │   │
│           │               │  │ ConfigMerge             │  │   │
│           │               │  └───────────┬─────────────┘  │   │
│           │               │              │                 │   │
│           │               │              ▼                 │   │
│           │               │  ┌─────────────────────────┐  │   │
│           │               │  │ Runbook Layer (增强)     │  │   │
│           │               │  │                         │  │   │
│           │               │  │ RunbookTemplateEngine   │  │   │
│           │               │  │ RunbookFeedbackAnalyzer │  │   │
│           │               │  │ [Phase 9+ optional]     │  │   │
│           │               │  │ RunbookWebSearcher      │  │   │
│           │               │  │ LLMRunbookGenerator     │  │   │
│           │               │  └───────────┬─────────────┘  │   │
│           │               │              │                 │   │
│           │               │              ▼                 │   │
│           │               │  ┌─────────────────────────┐  │   │
│           │               │  │ Existing Tool Backends  │  │   │
│           │               │  │ (Metrics/Logs/Trace/    │  │   │
│           │               │  │  K8s/DB/Deployment)     │  │   │
│           │               │  └─────────────────────────┘  │   │
│           │               └──────────────────────────────┘   │
│  ┌────────▼──────────┐                                       │
│  │ PostgreSQL + Redis │                                       │
│  └───────────────────┘                                       │
└──────────────────────────────────────────────────────────────┘
```

---

## 3. Auto-Discovery Layer

### 3.1 模块结构

```
packages/discovery/                    ← 新增模块
├── __init__.py
├── models.py           # DiscoveryResult, DiscoveredService, MetricMapping 等
├── k8s_discovery.py    # K8s API -> 服务列表、label 统计
├── prom_discovery.py   # Prometheus API -> 指标名、label values
├── loki_discovery.py   # Loki API -> label keys 交叉验证
├── metric_matcher.py   # 语义匹配引擎（核心）
├── topology.py         # WorkloadBinding + ServiceEdge 推导；Service selector 只生成绑定，不生成依赖边
├── runner.py           # 编排所有 discovery，合并结果
├── config_merge.py     # 多来源配置优先级合并
└── store.py            # DiscoveryRun / proposal / published config 持久化；生产用 DB，本地 demo 可选 JSON cache
```

### 3.2 启动数据流

```
独立 Celery task（启动后异步 / 周期 / 手动 rerun）
    │
    ▼
DiscoveryRunner.run()
    │
    ├──(1) K8sDiscovery
    │     ├── GET /api/v1/namespaces  -> namespace 列表
    │     ├── GET /api/v1/pods        -> 采样 Pod labels，统计 label key 分布
    │     ├── GET /apis/apps/v1/deployments  -> 服务列表
    │     ├── GET /apis/apps/v1/statefulsets -> 服务列表
    │     ├── GET /apis/apps/v1/daemonsets   -> 服务列表
    │     └── GET /api/v1/services    -> Service selectors（给 topology）
    │
    ├──(2) PromDiscovery
    │     ├── GET /api/v1/label/__name__/values -> 所有指标名
    │     ├── GET /api/v1/labels      -> 所有 label keys
    │     └── MetricMatcher.fuzzy_match(指标名列表) -> 语义映射
    │
    ├──(3) LokiDiscovery
    │     └── GET /loki/api/v1/labels -> 确认 label keys
    │
    └──(4) TopologyDeriver
          ├── Service selector -> WorkloadBinding
          ├── manual topology -> ServiceEdge (confidence=1.0)
          ├── Jaeger trace call graph -> ServiceEdge (confidence=0.8-0.95)
          ├── env var -> weak ServiceEdge (confidence=0.5-0.7)
          └── ConfigMap -> weak ServiceEdge (confidence=0.4-0.7)
              │
              ▼
         DiscoveryResult (merged, validated)
              │
              ▼
         写入 DiscoveryRun / proposal（DB）
              │
              ├── demo: 可按阈值合并为 EffectiveConfig
              └── production: 只能经 AutomationPolicy / review 发布后进入 worker
```

### 3.3 核心数据模型

```python
# packages/discovery/models.py

class DiscoveredService(BaseModel):
    name: str
    namespace: str
    kind: str                    # Deployment | StatefulSet | DaemonSet
    replicas: int | None = None
    labels: dict[str, str] = Field(default_factory=dict)
    ports: list[int] = Field(default_factory=list)

class MetricMapping(BaseModel):
    semantic_type: str           # latency | error_rate | qps | cpu | ...
    status: Literal["available", "degraded", "unavailable"]
    prometheus_metric: str | None = None       # 实际的 Prometheus 指标名；unavailable 时为空
    promql_template: str | None = None         # 参数化的 PromQL；unavailable 时为空
    confidence: float                          # 匹配置信度 0-1；unavailable 时为 0
    source: Literal["auto", "manual", "profile"]
    reason: str | None = None                  # unavailable/degraded 原因
    required_labels: list[str] = Field(default_factory=list)  # 该语义类型必需的 label
    missing_labels: list[str] = Field(default_factory=list)   # 缺失的 label
    last_validated_at: datetime | None = None  # 最后一次 dry-run 验证时间

class LabelConvention(BaseModel):
    """各可观测性后端的 service label 约定。K8s label 和 Prometheus/Loki/Trace 的 label 不一定同名。"""
    k8s_service_label: str | None = None       # K8s Pod label key，如 app.kubernetes.io/name
    metrics_service_label: str | None = None   # Prometheus 指标上的 service label
    logs_service_label: str | None = None      # Loki 日志流上的 service label
    trace_service_tag: str | None = None       # Jaeger trace span 上的 service tag
    confidence: float
    alternatives: list[dict] = Field(default_factory=list)  # [{key, coverage}]

class WorkloadBinding(BaseModel):
    """K8s Service 到 Workload 的归属关系。Service selector 只能推出这个，推不出服务间调用依赖。"""
    service: str
    workload: str                # Deployment / StatefulSet / DaemonSet 名称
    workload_kind: str           # "Deployment" | "StatefulSet" | "DaemonSet"
    evidence: str = "service_selector"

class ServiceEdge(BaseModel):
    """服务间的真实调用依赖边。Service selector 不能作为依赖边证据。"""
    source: str
    target: str
    evidence: Literal["manual", "trace_call_graph", "k8s_env_var", "configmap"]
    confidence: float                    # 按证据类型赋值：manual=1.0, trace=0.8-0.95, env=0.5-0.7, configmap=0.4-0.7
    reason: str | None = None

class ServiceTopology(BaseModel):
    services: list[str]
    edges: list[ServiceEdge]           # 服务间调用依赖
    bindings: list[WorkloadBinding] = Field(default_factory=list)  # Service→Workload 归属关系

class EnvironmentCapability(BaseModel):
    """各信号源的可用性状态。默认 unknown/false，探测成功后才标记 available。"""
    has_metrics: bool = False
    has_logs: bool = False
    has_traces: bool = False
    has_k8s: bool = False
    has_db_diagnostics: bool = False
    has_deployment_tracking: bool = False
    # 每类信号的详细状态（可选，用于更细粒度的降级信息）
    metrics_status: Literal["available", "degraded", "unavailable", "unknown"] = "unknown"
    logs_status: Literal["available", "degraded", "unavailable", "unknown"] = "unknown"
    traces_status: Literal["available", "degraded", "unavailable", "unknown"] = "unknown"

class DiscoveryResult(BaseModel):
    run_at: datetime
    label_convention: LabelConvention
    services: list[DiscoveredService]
    metric_mappings: list[MetricMapping]
    topology: ServiceTopology
    capabilities: EnvironmentCapability
    backends: BackendEndpoints
    primary_namespace: str | None = None       # 非 K8s/RBAC 不足/多 namespace 时可为 None
    warnings: list[str] = Field(default_factory=list)
    recommendations: list[str] = Field(default_factory=list)
    # 降级信号：明确列出不可用的能力和使用的回退信号
    capability_gaps: list[str] = Field(default_factory=list)       # 如 ["metrics.error_rate", "metrics.latency"]
    degraded_signals: list[str] = Field(default_factory=list)      # 如 ["prometheus_metric_mapping_unavailable"]
    used_fallback_signals: list[str] = Field(default_factory=list) # 如 ["logs", "traces", "k8s", "deployment", "runbooks"]
    confidence_adjustment: str | None = None  # 如 "downgraded_due_to_missing_metrics"
```

### 3.4 指标语义匹配引擎

内置语义模板库，每个语义类型对应一组候选正则模式：

```python
# 每个语义类型对应一组 MetricCandidate，包含 regex + label 要求 + PromQL 生成器
@dataclass
class MetricCandidate:
    regex: str                              # 匹配指标名的正则
    semantic_type: str                      # latency | error_rate | qps | cpu | ...
    required_any_labels: list[str] = field(default_factory=list)   # 至少存在一个
    required_label_value_pattern: str | None = None  # label 值的匹配模式（如 error_rate 需要 status=~"5.."）
    promql_builder: str | None = None       # 对应 PromQL 模板生成器名
    is_histogram: bool = False              # 是否为 histogram 类型（latency 类需要）
    metric_kind: Literal["counter", "gauge", "histogram", "summary", "unknown"] = "unknown"
    unit: Literal["seconds", "milliseconds", "bytes", "count", "ratio", "unknown"] = "unknown"

SEMANTIC_PATTERNS: dict[str, list[MetricCandidate]] = {
    "latency": [
        MetricCandidate(
            regex=r".*request.*duration.*bucket",
            required_any_labels=["le"],
            is_histogram=True, metric_kind="histogram", unit="seconds",
            promql_builder="histogram_quantile_latency",
        ),
        MetricCandidate(
            regex=r".*http.*duration.*bucket",
            required_any_labels=["le"],
            is_histogram=True, metric_kind="histogram", unit="seconds",
            promql_builder="histogram_quantile_latency",
        ),
        MetricCandidate(
            regex=r".*latency.*bucket",
            required_any_labels=["le"],
            is_histogram=True, metric_kind="histogram", unit="seconds",
            promql_builder="histogram_quantile_latency",
        ),
    ],
    "error_rate": [
        MetricCandidate(
            regex=r".*requests_total.*",
            required_any_labels=["status", "status_code", "code", "http_status"],
            # 注：schema 验证确认 label 存在即可；不在 discovery 时要求当前窗口必须有 5xx。
            # query 端通过 PromQL builder 处理空分子：分母有数据时分子 5xx 允许为空 → 按 0 处理。
            # 例如: sum(rate(metric{status=~"5.."}[5m])) / clamp_min(sum(rate(metric[5m])), 1)
            metric_kind="counter", unit="count",
            promql_builder="http_error_rate",
        ),
        MetricCandidate(
            regex=r".*http_errors.*",
            metric_kind="counter", unit="count",
            promql_builder="http_error_rate",
        ),
    ],
    "qps": [
        MetricCandidate(
            regex=r".*requests_total.*",
            metric_kind="counter", unit="count",
            promql_builder="rate_qps",
        ),
        MetricCandidate(
            regex=r".*throughput.*",
            metric_kind="counter", unit="count",
            promql_builder="rate_qps",
        ),
    ],
    "cpu_throttle": [
        MetricCandidate(
            regex=r".*cpu.*cfs_throttled.*",
            metric_kind="counter", unit="seconds",
            promql_builder="cpu_throttle_rate",
        ),
    ],
    "disk_avail": [
        MetricCandidate(
            regex=r".*filesystem.*avail.*",
            metric_kind="gauge", unit="bytes",
            promql_builder="disk_avail_bytes",
        ),
    ],
    # ... 更多语义类型
}
```

匹配算法：

```
对于每个语义类型:
  1. 用候选正则逐一匹配 available_metrics
  2. 按声明顺序作为优先级
  3. 对最佳匹配，调用 Prometheus /api/v1/series?match[]=<metric> 确认:
     a. 该 metric 确实存在有数据的 series
     b. 必需的 labels 是否存在（如 error_rate 需要 status/status_code/code）
     c. service_label 是否存在于该 metric 的 series 中
  4. 对满足 label 要求的候选，获取 Prometheus metadata:
     a. GET /api/v1/metadata?metric=<metric_name>
     b. 校验 type 是否符合 candidate.metric_kind（counter/gauge/histogram/summary）
     c. 校验 unit 是否与 candidate.unit 一致
     d. metadata 缺失时降级为 heuristic validation，不直接高置信自动发布

  5. 生成参数化 PromQL 并 dry-run（至少尝试多个时间窗口）:
     a. 多窗口 dry-run（二选一）:
        方式 A: GET /api/v1/query_range?query=<promql>&start=...&end=...&step=...
        方式 B: GET /api/v1/query，通过 PromQL range selector 分别生成 [5m]/[1h]/[6h] 变体
     b. 任一窗口有数据 -> query validation 通过
     c. 当前窗口为空但历史窗口有数据 -> 进入 `require_review` 或 `degraded`（不可自动发布为 available）
     d. 所有窗口均无数据 -> 降权；低流量服务/新服务/夜间时段不应直接标记 unavailable
     e. /series 有历史 series 且 metadata 校验通过 -> 至少标记 `degraded`，不直接 `unavailable`
     f. 确认 series 数量低于安全上限
     g. 确认查询耗时在可接受范围
     h. 校验 metric kind：histogram 必须有 le label；counter 适合 rate()；gauge 不应用 rate()
     i. 校验 unit：latency 类指标单位一致性；避免把毫秒指标当秒处理；返回值在合理范围

  6. 第一候选无数据或 label/metadata 不满足 -> fallback 到第二候选
  7. 所有候选都不满足 -> 该语义类型标记 status=unavailable，confidence=0
  8. 当前窗口 PromQL 非空 + metadata 校验通过的 candidate 才允许 confidence >= 0.90 自动发布
```

### 3.5 Service Label 检测

```python
class K8sDiscovery:
    async def detect_service_label(self) -> LabelConvention:
        """四路独立检测，不假设 k8s/metrics/logs/trace 使用同名 label。"""
        # 1. K8s: 从 Pod labels 统计 k8s_service_label
        #    候选: app, app.kubernetes.io/name, service, job, component, deployment, k8s-app, name
        #    选覆盖率 >= 80% 的最高频 key

        # 2. Prometheus: 从已匹配核心指标的 /api/v1/series 中检测 metrics_service_label
        #    不要求它和 k8s_service_label 同名

        # 3. Loki: 从 /loki/api/v1/labels 和样本 stream 中检测 logs_service_label

        # 4. Jaeger: 从 /api/services 或 span tags 中检测 trace_service_tag

        # 交叉验证（每个后端独立）:
        #    a. label key 在该后端全局存在
        #    b. label key 存在于该后端具体数据的 series/stream/span 中
        #    c. 不满足时降低 confidence 并记录 alternatives
```

### 3.6 拓扑推导

拓扑推导分为两层：**WorkloadBinding**（Service→Workload 归属）和 **ServiceEdge**（服务间调用依赖）。

#### WorkloadBinding 推导

K8s Service selector 只能推出 Service 到 Workload 的归属关系，不能推出服务间调用依赖：

1. **Service selector 匹配**：Service 的 selector 指向的 Pod → Pod 的 ownerRef → 对应的 Deployment/StatefulSet/DaemonSet
2. 输出为 `WorkloadBinding`，不作为 `ServiceEdge`

#### ServiceEdge 推导

四种策略，按可信度从高到低排序（高可信优先采用，低可信在无更好证据时作为参考）：

1. **手动拓扑文件**：`SERVICE_TOPOLOGY_PATH` 指向 JSON
   - 最高权威，confidence = 1.0

2. **Trace call graph 推导**：
   - 从 Jaeger trace 中提取真实服务间调用关系
   - 真实调用证据，confidence = 0.8-0.95
   - Tempo 待 Phase 9+ TempoTraceBackend 支持后启用

3. **环境变量注入推导**：
   - 扫描 Deployment spec 的 env，匹配 DNS 模式 `<svc>.<ns>.svc.cluster.local` 和 `*_SERVICE_HOST` 约定
   - 弱证据，confidence = 0.5-0.7

4. **ConfigMap 推导**：
   - 扫描挂载的 ConfigMap，匹配服务地址模式
   - 弱证据，confidence = 0.4-0.7

> **注意**：K8s Service selector 只生成 `WorkloadBinding`，不生成 `ServiceEdge`。它能推出 `Service → Workload`，但不能推出 `Service A → Service B`。

### 3.7 Backend Infrastructure Auto-Discovery

除了发现被诊断的服务，DiscoveryRunner 也自动定位可观测性基础设施。在 K8s 环境下，无需手动配置 Prometheus / Loki / Jaeger / Alertmanager 的地址。

> **当前阶段正式支持** Prometheus、Loki、Jaeger、Alertmanager 四个后端。
> Tempo、Grafana 的自动发现逻辑保留，但发现结果仅进入 `discovered_backends` 记录，不进入当前 worker 默认构造路径，待后续阶段实现对应 backend 后启用。

#### 发现流程

```
K8sDiscovery
    │
    ├── 1. 遍历候选 namespace
    │       monitoring, observability, loki, tempo, istio-system,
    │       kube-prometheus, prometheus, jaeger, grafana
    │
    ├── 2. 在每个 namespace 内 list services，匹配命名 pattern:
    │
    │       Pattern                   → 后端类型            状态
    │       ─────────────────────────────────────────────────────
    │       prometheus-*              → Prometheus          当前支持
    │       alertmanager-*            → Alertmanager        当前支持
    │       loki-*                    → Loki                当前支持
    │       jaeger-query*             → Jaeger Query        当前支持
    │       tempo-*                   → Tempo               后续扩展
    │       grafana-*                 → Grafana             后续扩展
    │
    ├── 3. 对每个候选 service，探测 health endpoint 确认身份:
    │
    │       当前支持:
    │       Prometheus:   GET /-/healthy                     → 200
    │       Loki:         GET /ready                         → 200
    │       Jaeger:       GET /api/services                  → 200
    │       Alertmanager: GET /api/v2/status                 → 200
    │
    │       后续扩展（发现但不启用）:
    │       Tempo:        GET /api/search?tags=service.name  → 200
    │       Grafana:      GET /api/health                    → 200
    │
    └── 4. 确认 → 当前支持的自动填充 BackendEndpoints
                  后续扩展的记录到 discovered_backends，status=detected_only，不进入 worker 默认构造路径
```

#### 数据模型

`BackendEndpoints` 和 `DiscoveredBackend` 的完整定义见 §3.7.1。本节只描述发现流程如何填充这些模型。

`DiscoveryResult`（见 §3.3）中 `backends: BackendEndpoints` 字段由基础设施发现流程填充。

#### 配置优先级

自动发现的地址优先级低于手动配置——用户显式设置 `.env` 中的 `PROMETHEUS_URL` 优先于 K8s 发现结果：

```python
def _resolve_backend(user_value, discovered_value, endpoint_type, app_env):
    """解析后端地址。生产环境禁止回退到默认 localhost。"""
    if user_value and is_explicitly_set(user_value, endpoint_type):
        return ResolvedValue(user_value, source="env")

    if discovered_value:
        return ResolvedValue(discovered_value, source="discovery")

    if app_env == "production":
        return UnresolvedValue(reason=f"{endpoint_type}_not_configured")

    # 仅非生产环境允许回退到内置默认值
    return ResolvedValue(defaults.get(endpoint_type), source="default")
```

#### 降级：非 K8s 或权限不足

```
K8s API 不可达
    │
    └── backends 全部标记 None，missing = ["prometheus", "loki", "jaeger", "alertmanager"]
        │
        ├── 如果用户配置了 PROMETHEUS_URL 等 → 正常启动
        │
        └── 如果用户也没配 → warnings 追加:
            "Run outside K8s and no backend URLs configured — agent will start
             but tool calls will fail. Set PROMETHEUS_URL, LOKI_URL, JAEGER_URL
             in .env or ensure K8s RBAC access."
```


### 3.7.1 后端认证配置

真实生产环境中，Prometheus、Loki、Jaeger、Alertmanager 通常需要认证。只配置 URL 是不够的。

```python
class BackendAuthConfig(BaseModel):
    """单类后端的认证配置。token 不持久化，运行时通过 env var 或 secret ref 注入。"""
    auth_type: Literal["none", "bearer_token", "basic_auth", "mtls", "service_account"] = "none"
    # Bearer token — 只能指定来源，不在配置中保存 token 值
    token_env_var: str | None = None       # 从环境变量读取
    secret_ref: str | None = None          # K8s Secret / vault path 引用
    # Basic auth
    username: str | None = None
    password_env_var: str | None = None
    # mTLS
    cert_file: str | None = None
    key_file: str | None = None
    ca_file: str | None = None
    # TLS
    tls_verify: bool = True
    tls_server_name: str | None = None     # SNI override
    # Timeout
    timeout_seconds: float = 10.0

class DiscoveredBackend(BaseModel):
    """单个被发现的后端。当前阶段只有 Prometheus/Loki/Jaeger/Alertmanager 可进入 enabled，
    Tempo/Grafana 发现后只能标记 detected_only。"""
    backend_type: Literal[
        "prometheus", "loki", "jaeger", "alertmanager",
        "tempo", "grafana",
    ]
    url: str | None = None
    status: Literal["enabled", "detected_only", "unsupported", "unavailable"]
    source: Literal["env", "discovery", "profile"]
    confidence: float = 1.0
    reason: str | None = None

class BackendEndpoints(BaseModel):
    """自动发现的可观测性基础设施地址。定义以本节为唯一权威来源。"""
    prometheus_url: str | None = None
    prometheus_auth: BackendAuthConfig = Field(default_factory=BackendAuthConfig)
    loki_url: str | None = None
    loki_auth: BackendAuthConfig = Field(default_factory=BackendAuthConfig)
    jaeger_url: str | None = None
    jaeger_auth: BackendAuthConfig = Field(default_factory=BackendAuthConfig)
    alertmanager_url: str | None = None
    alertmanager_auth: BackendAuthConfig = Field(default_factory=BackendAuthConfig)

    # 当前支持自动发现的后端（不一定是全部启动必需；缺失时可 degraded）
    auto_discovered: bool = False
    discovered_count: int = 0
    supported_backend_count: int = 4  # prometheus + loki + jaeger + alertmanager
    missing_required: list[str] = Field(default_factory=list)   # 启动必需但未发现的后端
    missing_optional: list[str] = Field(default_factory=list)   # 可降级后未发现的后端

    # 所有被发现的后端（含后续扩展；detail 格式，用于 UI 和审计）
    discovered_backends: list[DiscoveredBackend] = Field(default_factory=list)
```

Tempo/Grafana 发现结果示例：

```json
{
  "backend_type": "tempo",
  "url": "http://tempo.monitoring.svc.cluster.local:3200",
  "status": "detected_only",
  "source": "discovery",
  "confidence": 0.9,
  "reason": "Tempo backend is detected but TempoTraceBackend is scheduled for Phase 9+"
}
```

worker 构造工具时的硬规则：

```text
只有 status=enabled 的后端用于构造诊断工具。
detected_only 后端在 discovery status 中可见，但绝不进入 worker 默认构造路径。
```

K8s 集群内访问时优先使用只读 ServiceAccount；外部访问需要显式配置 token 或证书。所有 token、密钥、证书路径不得进入 LLM prompt。

### 3.8 Production-Safe Automation Policy

生产模式下，自动化不是 discovery 结果直接生效，而是由确定性策略判断是否可自动发布：

```bash
APP_ENV=production
AUTOMATION_LEVEL=supervised          # off | propose | supervised | autopilot（全局策略）
DISCOVERY_ENABLED=true
DISCOVERY_APPLY_MODE=inherit         # inherit | propose | supervised（只能 <= AUTOMATION_LEVEL）
RUNBOOK_TEMPLATE_GENERATION_ENABLED=true
RUNBOOK_LLM_GENERATION_ENABLED=false  # Runbook LLM 与诊断 LLM 分离，默认仍需显式开启
RUNBOOK_WEB_SEARCH_ENABLED=false
LLM_PROVIDER=disabled                # 生产默认不启用 LLM；operator 可选择 deepseek/openai/anthropic/vllm
# LLM_PROVIDER=deepseek              # 示例：如环境评审允许云端 LLM，取消注释并配置 LLM_API_KEY
# LLM_API_KEY=sk-xxx
# LLM_PROVIDER=vllm                  # 示例：本地 LLM
# LLM_BASE_URL=http://vllm:8000/v1
ALERT_SOURCE=poll                    # 后端项目零改动路径；webhook/both 也支持
EXECUTOR_BACKEND=fixture             # live 必须显式 operator opt-in
```

自动化等级语义：

| 等级 | 行为 |
|------|------|
| `off` | 不运行 discovery，不自动生成配置；仅使用 env/profile/default |
| `propose` | 自动生成候选结果，但不发布，不改变诊断工具配置 |
| `supervised` | **生产默认**。高置信、低风险、可回滚的配置自动发布；其他候选进入 review queue |
| `autopilot` | 在同样安全边界内尽可能自动发布；仍不绕过 guardrail、审批、L4 direct reject。必须显式 opt-in |

生产默认使用 `supervised` 而不是 `propose`。目标是减少首次接入的人工作业：只读采集配置（后端 URL、service label、metric mapping）可在高置信校验后自动发布；中低置信、冲突、缺证据、影响范围不清晰的 proposal 进入 review queue；所有写入类能力仍然默认关闭或需人工审批。`autopilot` 不作为生产默认。

`AUTOMATION_LEVEL` 为全局策略上限。`DISCOVERY_APPLY_MODE` 只能取 `inherit`（继承全局策略）或比 `AUTOMATION_LEVEL` 更保守的等级。例如：

- `AUTOMATION_LEVEL=supervised` + `DISCOVERY_APPLY_MODE=propose` → 允许（更保守）
- `AUTOMATION_LEVEL=propose` + `DISCOVERY_APPLY_MODE=supervised` → 不允许（更激进，DISCOVERY_APPLY_MODE 不能 > AUTOMATION_LEVEL）
- `DISCOVERY_APPLY_MODE=inherit` → 等同于 `AUTOMATION_LEVEL`

所有自动化输出统一进入策略判定：

```python
class AutomationDecision(BaseModel):
    decision: Literal["auto_apply", "require_review", "reject", "record_only"]
    reason: str
    confidence: float
    required_checks: list[str]
    failed_checks: list[str]
```

允许自动发布的条件，按配置类别细化：

**后端 URL（supervised 下可自动发布）：**
- 来源是 K8s service discovery（非手动猜测）；
- health endpoint 验证成功；
- 不覆盖显式 env 配置；
- 认证方式可用；
- 只影响只读工具。

**service label / namespace mapping（supervised 下可自动发布）：**
- 至少两个来源交叉验证一致，或一个权威来源 + PromQL/LogQL dry-run 成功；
- confidence >= 0.90；
- 最近连续 2 次 discovery 结果稳定；
- 无人工 override 冲突。

**metric mapping（supervised 下可自动发布）：**
- metric kind、label、unit、PromQL dry-run 均通过；
- 查询结果非空；
- series 数和响应大小低于阈值；
- confidence >= 0.90。

所有自动发布必须满足：
- 变更可以版本化、回滚，并写入 audit log。
- 任何不满足自动发布条件的 proposal 进入 review queue。

永远不能被 discovery/LLM/web search 自动发布或放宽的内容：

- `EXECUTOR_BACKEND=live`。
- L2/L3 审批要求和 L3 二次确认。
- L4 direct reject 策略。
- 真实数据库写入、缓存 flush、云资源写入。
- 未审核 Runbook 内容直接写入 `runbook_chunks`。



### 3.9 Production LLM Profiles

生产模式默认**不启用诊断 LLM**，必须由 operator 显式选择 profile。`LLM_PROVIDER=disabled` 是生产默认值，此时诊断只使用确定性证据和 Runbook 检索，不调用任何 LLM。如环境要求 LLM 辅助诊断，可配置为 `deepseek`、`openai`、`anthropic` 或本地 `vllm`。

| Profile | 配置 | 用途 | 安全边界 |
|---------|------|------|----------|
| 禁用 LLM（生产默认） | `LLM_PROVIDER=disabled` | 纯确定性诊断，不调用任何外部 LLM | 最安全；诊断基于规则、Runbook 检索和证据交叉验证 |
| 云端 LLM | `LLM_PROVIDER=deepseek`（示例）或 `openai` / `anthropic` + `LLM_API_KEY` | 生产诊断、摘要、hypothesis ranking | 不参与最终风险授权；prompt 必须经过压缩和脱敏，不携带未压缩大日志、Secret、token、认证头、完整 env 或不必要 PII |
| 本地 LLM | `LLM_PROVIDER=vllm` + `LLM_BASE_URL=http://.../v1` | 网络隔离或数据驻留要求较高的生产环境 | OpenAI-compatible API；同样不参与 guardrail 决策；需要本地容量和超时保护 |
| FakeLLM | `LLM_PROVIDER=fake` | CI、unit/integration smoke、离线 demo | 保持确定性，不作为生产诊断推荐 profile |

诊断 LLM 与 Runbook 生成 LLM 分离：生产诊断可以使用云端或本地 LLM；`RUNBOOK_LLM_GENERATION_ENABLED` 和 `RUNBOOK_WEB_SEARCH_ENABLED` 默认仍为 false，因为它们会引入知识写入和外部网页输入风险。开启时只能生成 draft，不能直接发布 Runbook 或改变执行权限。`LLM_PROVIDER=openai/deepseek/vllm` 使用 OpenAI-compatible chat completions 适配；`LLM_PROVIDER=anthropic` 使用 Anthropic Messages API 适配。

无论使用云端还是本地 LLM：

- Guardrail、risk level、L2/L3 approval、L4 direct reject 仍由确定性代码决定。
- 无 Prometheus 或缺少关键证据时，LLM 只能输出 hypothesis / missing_evidence，不能把推断升级为 confirmed root cause。
- LLM 输出必须引用 evidence ID、Runbook chunk ID 或明确标记为 hypothesis。
- 云端 LLM 调用前必须执行 redaction，记录 `redaction_applied=true`；如果无法完成脱敏，应降级到本地 LLM/FakeLLM 或跳过 LLM reasoning。
- CI、稳定测试和 smoke eval 继续固定使用 FakeLLM。


### 3.10 Discovery 查询成本控制

大规模 Prometheus 和 K8s 集群中，直接拉全量指标名、全量 label、全量 pods 可能对监控系统造成压力。

```python
class DiscoveryCostControl(BaseModel):
    """Discovery 查询的成本控制参数"""
    # 通用
    query_timeout_seconds: float = 10.0
    cache_ttl_seconds: int = 300              # 5 分钟缓存

    # Prometheus
    max_metric_names: int = 5000              # /api/v1/label/__name__/values 最大指标数
    max_series_per_query: int = 10000         # 单次 /series 查询最大 series 数
    max_label_values_per_label: int = 1000    # 单个 label 的最大 value 数
    dry_run_timeout_seconds: float = 5.0      # PromQL dry-run 超时

    # K8s
    max_pods: int = 5000                      # 最大 Pod 数
    max_namespaces: int = 100                 # 最大 namespace 数
    namespace_allowlist: list[str] = Field(default_factory=list)  # namespace 白名单
    service_allowlist: list[str] = Field(default_factory=list)    # service 白名单
    pod_sample_ratio: float = 1.0            # Pod 采样比例（大集群可降低）

    # Loki
    loki_label_query_timeout_seconds: float = 5.0
```

控制机制：

| 机制 | 说明 |
|------|------|
| Query timeout | 每类后端 API 调用设置超时，超时记为 degraded |
| 范围收敛与截断 | 优先使用 `start`、`end`、`match[]`、`limit` 限制 Prometheus label/series 查询范围；Prometheus 原生 API 不按 page/page_token 分页，超过上限时客户端截断并记录 warning |
| 结果数上限 | 超过上限时截断并记录 warning，不阻塞 discovery |
| 缓存 | 同一 query 在 TTL 内不重复请求；缓存 key = hash(endpoint + query) |
| 范围限制 | `namespace_allowlist`、`service_allowlist` 限制 discovery 范围 |
| 采样 | 大集群可降低 Pod 采样比例，减少 K8s API 压力 |
| 顺序控制 | label values 和 series 查询放在指标名匹配之后，减少不必要的 API 调用 |

Discovery 失败不阻塞 agent 启动，只记录 degraded。成本控制参数可根据集群规模调整。

---

## 4. 降级策略

设计原则：**逐级降级，不整体失败**。Discovery 的局部失败不阻塞 agent 启动。

### 4.1 逐组件降级

| 组件 | 失败原因 | 降级行为 |
|------|---------|---------|
| K8s API | 不在 K8s 内 / RBAC 不足 | services 列表为空，label 回退到 Prometheus 探测，topology 回退到静态文件 |
| Prometheus | 网络不通 / 认证失败 | metric_mappings 全部标记 unavailable；MetricsTool 返回 degraded/unavailable，诊断只能写 hypothesis / missing_evidence，不用默认 PromQL 伪造可复查证据 |
| 单个指标 | 该环境不采集此指标 | 该语义类型标记 confidence=0，证据交叉验证自动降权 |
| Label 模糊 | 无 key 覆盖率 > 80% | 取最高覆盖率候选，confidence 低于阈值时标记需人工确认 |
| Loki | 不可达 | logs_service_label 沿用 metrics label，日志节点返回 degraded |
| 拓扑推导 | 无 env var / 无 Service selector | topology 为空，diagnose 跳过级联分析，不影响单服务诊断 |
| 基础设施发现 | 不在 K8s 内 / RBAC 不足 | backends 全部标记 missing；只回退到显式 `.env` / profile，生产未配置的后端工具返回 degraded，不把 localhost/default 当成真实证据源 |
| Alertmanager | 地址未找到 / 不可达 | Pull 模式降级为 disabled，如有 webhook 仍可 push 接收告警 |

### 4.2 Discovery Health 输出

```
Discovery Result (2026-06-10 14:32:01 UTC)

  OK 6/8  metric types matched
  OK K8s: 12 services discovered
  WARN  Label confidence: 0.40 (review recommended)
  WARN  2 metrics unavailable: db_connections, cache_hit_rate
  WARN  Topology: not derived (no env var patterns)

  Overall: DEGRADED -- agent functional

  Recommendations:
  1. Set METRICS_SERVICE_LABEL=app if correct
  2. Provide topology at SERVICE_TOPOLOGY_PATH
```


### 4.3 后端项目零改动且关键指标缺失时的自动化能力

生产接入时允许后端项目不新增业务指标、不修改 label、不改日志格式。此时 agent 的自动化能力按“已有信号”分层启用；缺失的语义指标只降低诊断置信度，不阻塞事故流程。

| 能力 | 后端未暴露标准指标时是否可自动化 | 数据来源 | 生产安全边界 |
|------|----------------------------------|----------|--------------|
| 告警接入与去重 | 可以 | Alertmanager webhook 或 Alertmanager poll（无需改 Alertmanager 配置）；Grafana webhook 后续扩展 | 仍走 `AlertService.create_alert()`，使用 fingerprint 去重；poll 需要只读访问、分布式锁、过滤、限流和审计 |
| Incident 创建与 Celery 诊断入队 | 可以 | API + DB | API 不 inline 跑 LangGraph，保持幂等 |
| 服务/namespace/deployment 识别 | 可以 | K8s read-only API、profile、alert labels | namespace allowlist；不保存 Secret、完整 env、完整 ConfigMap |
| 日志聚合与错误签名 | 可以，若 Loki 有可筛选 label | Loki label/query_range | 限制时间窗、limit、keywords；大日志压缩，不直接进 prompt |
| Trace 慢调用和下游依赖分析 | 可以，若 Jaeger 有 trace | Jaeger read API（Tempo 后续扩展） | 限制 span 数和查询窗口；只读 |
| 部署变更关联 | 可以，若 GitHub/Argo CD 可读或 fixture/profile 可用 | Deployment backend | 只读；缺失时降级为无部署证据 |
| K8s 诊断 | 可以 | live read-only K8s backend | 只允许 describe/logs/events/rollout status/get deployment |
| DB 诊断 | 可以，若提供只读 DSN | 固定 SELECT 模板 | read-only transaction、statement timeout、拒绝非 SELECT |
| Runbook 检索 | 可以 | 已审核入库的 runbook chunks | 必须保留 chunk_id/source_path；未审核 draft 不参与高风险动作依据 |
| Runbook 模板草稿 | 可以 | discovery/profile/capability matrix | 自动生成 draft；人工审核后版本化再 ingest |
| Incident 报告 | 可以 | 已采集证据、降级信息、动作结果 | 明确列出 unavailable/degraded 信号，不伪造指标 |
| L0/L1 自动动作 | 可以按现有 guardrail | 确定性 policy | 默认 fixture executor；生产 live 写入不因指标缺失而放宽 |
| L2/L3 审批包生成 | 可以 | 证据、dry-run、snapshot、rollback plan | 自动准备，人工审批；L3 仍需二次确认 |
| error_rate/latency/SLO burn 等指标诊断 | 不能直接自动计算 | Prometheus 指标缺失 | 标记 `unavailable`，诊断降权；可使用 alert payload 中已有数值作为 alert evidence |
| 基于缺失指标的根因结论 | 不能自动确认 | 无可靠数据 | 只能提出 hypothesis/missing_evidence，不得作为 confirmed root cause；无 Prometheus 时默认写成 hypothesis |

指标缺失时，自动化诊断输出必须包含：

```json
{
  "capability_gaps": ["metrics.error_rate", "metrics.latency"],
  "degraded_signals": ["prometheus_metric_mapping_unavailable"],
  "used_fallback_signals": ["logs", "traces", "k8s", "deployment", "runbooks"],
  "confidence_adjustment": "downgraded_due_to_missing_metrics"
}
```

关键规则：

- 缺失指标不会阻塞告警接入、诊断编排、审批、报告和 Runbook 草稿自动化。
- 缺失指标会阻止系统自动确认依赖该指标的根因，例如 SLO burn、rate limit hits、queue lag、CPU throttle。
- 如果 alert payload 自带当前值、阈值和表达式，可作为 `alert evidence` 使用，但不能等同于 Prometheus 可复查指标。
- 任何由日志/trace/K8s 推断出的结论都要引用对应 evidence ID，并标记 signal source。


---

## 5. 配置优先级系统

```
优先级（高->低）:

1. 环境变量 (.env)           <- 用户显式设定，最高权威
2. Profile 文件              <- 用户选择的预设 profile
3. Discovery proposal / published config <- demo 可按阈值采纳；生产只读取已发布版本
4. 代码内置默认值            <- settings.py 中的 Field(default=...)
```

```python
# packages/discovery/config_merge.py

def merge_settings(settings: Settings, discovery: DiscoveryResult | None) -> EffectiveConfig:
    """按优先级合并配置来源。"""
    return EffectiveConfig(
        metrics_service_label=_resolve(
            user=settings.metrics_service_label,
            discovered=discovery.label_convention.metrics_service_label if discovery else None,
            confidence=discovery.label_convention.confidence if discovery else 0,
            default="service",
        ),
    )
```

**置信度阈值**：本地 demo 可使用 `>= 0.8` 自动采纳、`0.5-0.8` 采纳但 warn、`< 0.5` 回退默认值；生产模式使用 5.1 的 published effective config 和 3.8 的 AutomationPolicy，建议自动发布阈值 `>= 0.90`。


### 5.1 生产模式配置发布规则

上面的优先级适用于本地 demo 和手动 profile 合并。生产模式下必须使用带来源和版本的 effective config，不能让 worker 直接读取未审核的 discovery 缓存。

生产优先级（高 -> 低）：

1. 环境变量（显式 operator 配置）。
2. 有效期内的人工 override。
3. 已发布的 `EffectiveConfigVersion`。
4. 用户选择的 profile。
5. 未发布的 discovery proposal（仅用于 UI/review，不进入 worker）。
6. 代码默认值（仅用于安全默认值；生产后端 URL、指标映射、poll 范围过滤缺失时返回 unavailable/degraded，不把默认 localhost 或低置信候选当成真实证据源）。

每个配置字段都应保留来源元数据：

```json
{
  "metrics_service_label": {
    "value": "app",
    "source": "published_discovery",
    "confidence": 0.94,
    "config_version_id": "cfg_123",
    "validated_at": "2026-06-10T14:32:01Z"
  }
}
```

worker 构造工具时只读取当前 `published` 的 effective config version，并把 `config_version_id` 写入 agent run state/debug snapshot。这样事故复盘可以复现当时使用的 label、PromQL、LogQL 和后端 URL。


### 5.2 审计日志结构

所有配置变更（发布、回滚、override、自动发布、拒绝发布）必须写入结构化审计日志：

```python
class AuditLogEntry(BaseModel):
    id: str                              # aud_xxx
    actor: str                           # "system" | "user:<username>" | "automation"
    action: Literal[
        "config.publish",
        "config.rollback",
        "config.revoke",
        "config.override.create",
        "config.override.expire",
        "discovery.auto_apply",
        "discovery.require_review",
        "discovery.reject",
        "discovery.record_only",
    ]
    target: str                          # 变更对象：config_version_id / override_id / proposal_id
    before: dict | None = None           # 变更前状态（可序列化摘要）
    after: dict | None = None            # 变更后状态（可序列化摘要）
    reason: str                          # 变更原因（必填）
    source: str                          # 触发来源：env / profile / discovery / manual_override
    request_id: str                      # 关联的 API request ID
    created_at: datetime                 # UTC
    metadata: dict = {}                  # 扩展字段：confidence、decision、failed_checks 等
```

审计日志写入要求：

- 不可变：创建后不可修改或删除。
- 可查询：支持按 actor、action、target、时间范围筛选。
- 与 agent run 关联：诊断运行时使用的 `config_version_id` 可追溯到对应的 audit log entry。
- 生产环境审计日志保留期建议 >= 90 天。

### 5.3 配置版本过期策略

`EffectiveConfigVersion` 应有生命周期管理，防止旧 discovery 结果长期生效：

```python
class EffectiveConfigVersion(BaseModel):
    id: str                              # cfg_xxx
    status: Literal["draft", "published", "expired", "revoked"]
    published_at: datetime | None = None
    validated_at: datetime | None = None
    expires_at: datetime | None = None   # 过期时间；发布时设置
    revoked_at: datetime | None = None
    revoked_by: str | None = None
    staleness_policy: str = "expire_after_days"  # 过期策略类型
    staleness_days: int = 30             # 建议 30 天后过期
    source_proposal_id: str | None = None
    diff_from_previous: dict | None = None
    rollback_target_id: str | None = None
```

过期行为：

- 过期后 worker 不再使用该版本，回退到显式 env/profile。
- 不会自动回退到未发布的 discovery proposal。
- 过期前 N 天（建议 7 天）发出 warning，提醒 operator 重新运行 discovery 并发布新版本。
- 手动 override 可以延长过期时间，但需要审计记录。


---

## 6. Runbook 生成

### 6.1 分层生成策略

Runbook 生成分两个阶段交付。第一阶段（Phase 4）只做确定性模板生成；LLM + Web Search 拆分到后续阶段（Phase 9+），因为实现复杂度高且安全边界多（脱敏、SSRF 防护、搜索审计、来源追溯、review workflow）。

```
DiscoveryResult (服务名、指标映射、能力矩阵、拓扑)
        │
        └──> 第一阶段 (Phase 4): 模板填充 (确定性, 100% 可用)
                Jinja2 模板 + discovery 变量 -> 基础 Runbook 骨架
                标记 source=template, confidence=medium
                自动生成 draft；人工审核后版本化再 ingest

        └──> 第二阶段 (Phase 9+): LLM + Web Search Tool Use (可选, 默认关闭)
                RUNBOOK_LLM_GENERATION_ENABLED=false
                RUNBOOK_WEB_SEARCH_ENABLED=false
                LLM 拥有 web_search 工具，可在生成过程中主动搜索
                循环: 生成段落 -> 发现缺口 -> 搜索 -> 补充 -> 继续
                输出: RunbookDraft (正文 + SelfCritique + 完整搜索追溯)
                只能生成 draft，不能直接发布 Runbook 或写入 runbook_chunks
```

### 6.2 Runbook 模板结构

```
runbooks/
├── _templates/              <- 通用模板（项目内置）
│   ├── db-connection-exhaustion.md.j2
│   ├── high-5xx-after-deploy.md.j2
│   ├── pod-restart-loop.md.j2
│   ├── high-latency.md.j2
│   └── redis-cache-avalanche.md.j2
│
├── _feedback/               <- 反馈摘要（改进生成质量）
│   └── {fault_type}.json
│
└── <service-name>/          <- 实例化后的 runbook
    ├── .meta.yaml           <- 生成时间、review 状态
    └── db-connection-exhaustion.md
```

模板使用 Jinja2 语法，变量来自 discovery 结果。能力矩阵控制段落可见性：

```markdown
{# runbooks/_templates/db-connection-exhaustion.md.j2 #}
# Database Connection Exhaustion

## Symptoms
- Prometheus: `{{ metrics.db_connections }}` > 80% of max pool size
{% if has_traces %}
- Traces: spans 在 DB 调用处出现 timeout
{% endif %}

## Diagnostic Steps
1. 检查 `{{ metrics.db_connections }}` 当前值及趋势
{% if has_k8s %}
2. 检查 Pod 资源: `kubectl describe pod -l {{ service_label }}={{ service_name }}`
{% endif %}
...
```

### 6.3 LLM + Web Search Tool Use

生产约束：`RUNBOOK_LLM_GENERATION_ENABLED=false`、`RUNBOOK_WEB_SEARCH_ENABLED=false` 是默认值。开启后也只能生成 `RunbookDraft`，不能直接发布 Runbook、写入 `runbook_chunks`、改变执行权限或作为 L2/L3 动作的唯一依据。搜索 query 必须去除真实服务名、Secret、token、账号、客户标识和内部域名；`fetch_url` 只允许 `http/https`，拒绝内网 IP、link-local、metadata endpoint、`file://`、二进制下载和 JS 执行，并设置大小、超时、重定向和 MIME type 限制。

#### 工具定义

LLM 在生成 Runbook 时可调用以下工具：

```python
# packages/discovery/runbook_tools.py

class WebSearchTool:
    """LLM 可在生成过程中随时调用的搜索工具。"""

    name = "web_search"
    description = (
        "Search the web for runbook best practices, known issues, "
        "postmortems, or configuration references. Use when you need "
        "to verify a detail or find information not covered by discovery."
    )

    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search query in natural language or keywords"
            },
            "purpose": {
                "type": "string",
                "description": "Why you are searching — helps trace the reasoning chain"
            },
            "max_results": {
                "type": "integer",
                "default": 5,
                "description": "Number of results to return"
            },
        },
        "required": ["query", "purpose"],
    }

    async def execute(self, query: str, purpose: str, max_results: int = 5) -> SearchToolResult:
        # 1. 搜索
        raw = await self.searcher.search(query, max_results)
        # 2. 对高相关度结果抓取全文摘要
        enriched = await self._enrich(raw)
        # 3. 返回结构化结果 + URL 供最终追溯
        return SearchToolResult(
            query=query,
            purpose=purpose,
            results=enriched,
        )


class FetchUrlTool:
    """LLM 可在需要深读某个页面时调用。"""

    name = "fetch_url"
    description = "Fetch and extract the full content of a URL for detailed reading."

    parameters = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "The URL to fetch"},
            "reason": {"type": "string", "description": "Why you need to read this page"},
        },
        "required": ["url", "reason"],
    }
```

#### 生成循环

```
LLMRunbookGenerator.generate(fault_type, discovery_context)

    输入:
    - fault_type: 故障类型名
    - discovery_context: 服务名、指标映射、能力矩阵、拓扑
    - tools: [web_search, fetch_url]

    循环 (最多 N 轮):

    第 1 轮: LLM 收到 System Prompt + Discovery 上下文 + 模板骨架
         LLM 开始生成 Runbook 草稿
         |
         写 Symptoms 段落 -> OK（discovery 中有足够信息）
         |
         写 Root Causes 段落 -> LLM: "我不确定这个特定 PG 版本的最大连接数默认值"
         |
         LLM 调用 web_search(
             query="PostgreSQL 15 max_connections default value configuration",
             purpose="verify default value for root cause section"
         )
         |
         搜索结果返回 -> LLM 收到 {"PostgreSQL 15 默认 max_connections=100", ...}
         |
         LLM 继续写 Root Causes，引用搜索结果

    第 2 轮: LLM 继续生成 Remediation 段落
         |
         LLM: "这个中间件的已知 bug 我需要确认"
         |
         LLM 调用 web_search(
             query="PgBouncer transaction mode connection leak bug CVE",
             purpose="check known PgBouncer issues for remediation section"
         )
         |
         发现相关 CVE -> LLM 调用 fetch_url(url="https://...", reason="read full CVE details")
         |
         获取完整信息 -> LLM 写入 Remediation 段落，附引用

    第 N 轮: LLM 完成草稿
         |
         LLM 生成 Self-Critique
         |
         输出: RunbookDraft (包含完整 search_trail)
```

#### System Prompt

```
System:
You are an SRE expert writing runbooks for a production service.
Generate a concise, actionable runbook for the specified fault type.

You have access to:
- web_search: search the web for runbook best practices, known issues, configuration
  references, and postmortems
- fetch_url: fetch and read the full content of a URL for details

Rules:
- Use ACTUAL metric names from the Discovery context, never invent placeholders
- Only include diagnostic steps executable with the available tools
- When you are uncertain about a detail (version-specific defaults, known bugs,
  configuration parameters), use web_search to verify rather than guessing
- Always record the *purpose* of each search — this becomes part of the audit trail
- Cite sources in the runbook body where they contributed specific information
- Stop searching when you have enough to write confidently (max 8 tool calls per draft)
- After the runbook, append a Self-Critique section

Discovery Context:
- Service: {service_name} (namespace: {namespace})
- Available Tools: {capabilities}
- Metric Mappings: {actual_metric_name -> semantic_type}
- Service Dependencies: {topology}
- Template Skeleton: {template_outline}
```

#### 搜索追溯

每次 tool call 都被记录，最终作为 RunbookDraft 的一部分呈现给审查者：

```json
{
  "search_trail": [
    {
      "round": 1,
      "tool": "web_search",
      "query": "PostgreSQL 15 max_connections default value configuration",
      "purpose": "verify default value for root cause section",
      "results_count": 5,
      "used_in_section": "Root Causes",
      "citations": [
        {"url": "https://postgresql.org/docs/15/runtime-config-connection.html", "title": "PostgreSQL Docs - Connection Settings"}
      ]
    },
    {
      "round": 2,
      "tool": "web_search",
      "query": "PgBouncer transaction mode connection leak bug CVE",
      "purpose": "check known PgBouncer issues for remediation section",
      "results_count": 3,
      "used_in_section": "Remediation",
      "citations": [
        {"url": "https://github.com/pgbouncer/pgbouncer/issues/1234", "title": "PgBouncer #1234 - Connection leak in transaction mode"}
      ]
    }
  ]
}
```

### 6.4 RunbookDraft 数据结构

```python
class SearchTrailEntry(BaseModel):
    """LLM 每次 tool call 的完整记录"""
    round: int                         # 第几轮 tool call
    tool: Literal["web_search", "fetch_url"]
    query: str                         # 搜索查询或 URL
    purpose: str                       # LLM 解释为什么搜这个
    results_count: int
    used_in_section: str               # 结果用在了 Runbook 哪个段落
    citations: list[Citation] = Field(default_factory=list)     # 引用的 URL

class Citation(BaseModel):
    url: str
    title: str

class SelfCritique(BaseModel):
    confidence: Literal["high", "medium", "low"]
    weaknesses: list[str]
    needs_human_verification: list[str]
    suggested_improvements: list[str]

class DraftSource(BaseModel):
    source_type: Literal["discovery", "web_search", "template", "llm_inference"]
    description: str
    url: str | None = None
    raw_snippet: str | None = None

class RunbookDraft(BaseModel):
    fault_type: str
    service_name: str
    markdown_content: str
    self_critique: SelfCritique
    sources: list[DraftSource]
    search_trail: list[SearchTrailEntry] = Field(default_factory=list)  # 完整搜索链路
    tool_call_count: int = 0                   # 总 tool call 次数
    generated_at: datetime
    model: str
    status: Literal["pending_review", "reviewed", "rejected"] = "pending_review"
```

### 6.5 搜索成本控制

| 机制 | 说明 |
|------|------|
| 单草稿最大 tool call 数 | 默认 8 次，超过后 LLM 必须基于现有信息完成 |
| 搜索结果缓存 | 同一 query 在 24h 内不重复搜索 |
| 搜索超时 | 单次搜索 5 秒超时，失败不阻断生成 |
| 搜索并发 | 同一轮内多个独立搜索可并行发出 |

```python
class RunbookGenerationConfig:
    max_tool_calls_per_draft: int = 8
    search_cache_ttl_seconds: int = 86400     # 24h
    search_timeout_seconds: float = 5.0
    max_parallel_searches: int = 3
```

### 6.6 审查 API

```
GET  /api/runbooks/drafts?service={name}
     -> 待审查草稿列表（按 confidence 排序，低置信度优先）

GET  /api/runbooks/drafts/{draft_id}
     -> 单个草稿完整内容 + 自评 + 信息源追溯

POST /api/runbooks/drafts/{draft_id}/review
     -> {"decision": "approve" | "edit" | "reject", "edited_content": "...", "comment": "..."}

POST /api/runbooks/regenerate
     -> {"service": "...", "fault_types": ["..."]}
     -> Phase 4-8：重新触发确定性模板生成
     -> Phase 9+ 且 RUNBOOK_LLM_GENERATION_ENABLED=true 时，才允许触发 LLM/Web Search draft 生成
```

---

## 7. Runbook Feedback Loop

Phase 5 只做确定性反馈收集，不调用 LLM，不调用 web_search。LLM 差异分析和 web_search 验证延后到 Phase 9+。

### 7.1 流程

```
Incident 完成
      │
      ├──> persist_memory (现有, 不改)
      │      └── L0-L3 Memory
      │
      └──> RunbookFeedbackAnalyzer (新增, Phase 5)
              │
              ├── 条件触发:
              │     ├── 同类故障累计 >= 3 次
              │     ├── 根因置信度 >= 0.7
              │     └── 有实际成功执行的动作
              │
              ├── 确定性分析（不调用 LLM）:
              │     ├── 聚合同类 incident
              │     ├── 统计成功/失败/跳过/拒绝的动作
              │     ├── 识别 Runbook 中缺失的 fault type、service、diagnostic step
              │     ├── 识别反复出现的 evidence pattern
              │     └── 生成 RunbookFeedbackSummary + AmendmentDraft 框架
              │
              └── -> 人工审批 -> 创建 RunbookVersion -> reingest -> 更新 runbook_chunks 索引
```

### 7.2 确定性反馈数据结构

```python
class RunbookFeedbackSummary(BaseModel):
    """确定性反馈摘要。Phase 5 生成，不依赖 LLM。"""
    service: str
    fault_type: str
    incident_count: int
    successful_actions: list[str] = Field(default_factory=list)
    failed_actions: list[str] = Field(default_factory=list)
    skipped_actions: list[str] = Field(default_factory=list)
    rejected_actions: list[str] = Field(default_factory=list)
    missing_sections: list[str] = Field(default_factory=list)       # Runbook 中缺失的段落
    missing_fault_types: list[str] = Field(default_factory=list)    # 有 incident 但无对应 Runbook 的 fault type
    recurring_evidence_patterns: list[str] = Field(default_factory=list)
    first_seen_at: datetime
    last_seen_at: datetime
    confidence: float  # 基于样本量的统计置信度
    recommendation: Literal[
        "no_change",
        "review_existing_runbook",
        "create_new_runbook",
        "consider_amendment",
    ]

class AmendmentDraft(BaseModel):
    """待人工补充的 Amendment 框架。Phase 5 生成框架，Phase 9+ 可由 LLM 填充 proposed_text。"""
    service: str
    fault_type: str
    source: Literal["deterministic_feedback"] = "deterministic_feedback"
    based_on_incidents: list[str] = Field(default_factory=list)
    suggested_sections: list[str] = Field(default_factory=list)   # 建议新增/修改的段落名
    evidence_summary: str                                          # 确定性统计摘要
    status: Literal["pending_review", "rejected", "converted_to_amendment"] = "pending_review"
    created_at: datetime
    reviewed_by: str | None = None
    reviewed_at: datetime | None = None

class RunbookAmendment(BaseModel):
    """完整的 Amendment。Phase 5 可生成确定性框架，Phase 9+ 可由 LLM 补全 proposed_text；必须人工审批后才能 apply。"""
    id: str
    service: str
    fault_type: str
    source: Literal["deterministic_feedback", "llm_analysis"]
    proposed_at: datetime
    additions: list[AmendmentItem] = Field(default_factory=list)
    modifications: list[AmendmentItem] = Field(default_factory=list)
    deprecations: list[AmendmentItem] = Field(default_factory=list)
    based_on_incidents: list[str] = Field(default_factory=list)
    search_trail: list[dict] = Field(default_factory=list)   # Phase 9+ 填充
    confidence: float
    rationale: str
    status: Literal["pending", "approved", "rejected", "applied"] = "pending"
    reviewed_by: str | None = None
    applied_at: datetime | None = None

class AmendmentItem(BaseModel):
    section: str                      # Symptoms / Root Causes / Diagnostic Steps / Remediation
    original_text: str | None = None  # 修改场景下的原文
    proposed_text: str | None = None  # Phase 5 可为空（待人工填写），Phase 9+ LLM 可生成
    evidence_from_incidents: list[str] = Field(default_factory=list)
    priority: Literal["critical", "high", "medium", "low"]
```

### 7.3 频率控制

```bash
RUNBOOK_AMENDMENT_COOLDOWN_DAYS = 7     # 同 fault_type 至少间隔 7 天才重新分析
RUNBOOK_AMENDMENT_MIN_INCIDENTS = 3     # 累计 >= 3 次同类事故才触发
```

### 7.4 Phase 9+ LLM 增强（后续）

Phase 9+ 才启用 LLM 差异分析与 web_search。该能力只用于增强 Phase 5 产生的确定性反馈摘要：

- LLM 对比现有 Runbook 与历史 incident，生成 proposed amendment text。
- web_search 验证外部资料、版本差异、最佳实践。
- fetch_url 深读官方文档或公开 issue。
- 生成带 citations 和 search_trail 的 `RunbookAmendment`。
- 输出仍然是 draft，必须人工审批后才能创建 `RunbookVersion` 并 reingest。

安全边界不变：`RUNBOOK_LLM_GENERATION_ENABLED=false`、`RUNBOOK_WEB_SEARCH_ENABLED=false` 默认保持关闭。

---

## 8. Discovery API

```
GET  /api/discovery/status
     -> 最后一次 DiscoveryResult 摘要（运行时间、健康、warnings、置信度）

GET  /api/discovery/services
     -> 发现的服务列表 [{name, namespace, kind, replicas}]

GET  /api/discovery/metrics
     -> 指标映射表 [{semantic_type, prometheus_metric, confidence}]

GET  /api/discovery/topology
     -> 服务拓扑图 {services: [...], edges: [{source, target, evidence}]}

GET  /api/discovery/capabilities
     -> EnvironmentCapability 矩阵

POST /api/discovery/rerun
     -> 触发重新探测（异步，返回 task_id）

POST /api/discovery/override
     -> 创建人工 override，必须写审计字段
     -> Body: {
          "key": "metrics_service_label",
          "value": "my_label",
          "scope": {"namespace": "prod", "service": "checkout"},
          "reason": "Prometheus label convention verified by operator",
          "expires_at": "2026-07-10T00:00:00Z"
        }

POST /api/discovery/effective-config/publish
     -> 发布一个 EffectiveConfigVersion；Body 必须包含 proposal_id/config_diff/reason

POST /api/discovery/effective-config/{config_version_id}/rollback
     -> 回滚到指定已发布版本；写 audit log，不删除历史版本

POST /api/discovery/effective-config/{config_version_id}/revoke
     -> 撤销当前版本；生产 worker 只能回退到显式 env/profile，不能回退到未发布 discovery proposal
```

---

## 9. DiscoveryRunner 集成点

生产模式不在 `_build_deps()` 内同步执行 discovery。DiscoveryRunner 作为独立 Celery task（启动后、周期或手动 rerun）生成 proposal / published effective config；worker 构造依赖时只读取已发布配置。

```python
# apps/worker/tasks.py

def _build_deps(db, settings, agent_run_id, incident_id):
    published = load_published_effective_config(db, settings.environment)

    if settings.app_env == "production":
        # 生产只允许显式 env/profile + 当前 published config。
        # 未发布 discovery proposal 不进入诊断路径。
        effective = EffectiveConfig.from_operator_sources(
            settings=settings,
            published=published,
            allow_discovery_proposals=False,
        )
    else:
        effective = EffectiveConfig.from_demo_sources(
            settings=settings,
            latest_discovery=load_latest_discovery_proposal(db),
        )

    # 缺失的生产数据源标记 degraded/unavailable，而不是回退到默认 localhost 或低置信猜测。
    if settings.app_env == "production" and effective.has_unresolved_required_sources():
        record_degraded_tool_sources(agent_run_id, effective.unresolved_sources())

    # 构造 tools（用 effective config 替代 settings 默认值）
    metrics_tool = MetricsTool(
        base_url=effective.prometheus_url,
        service_label=effective.metrics_service_label,
        metric_patterns=effective.metric_patterns,
        ...
    )
```

---

## 10. Alert Pull Mode

除了传统的 Alertmanager webhook push，agentp 支持主动轮询模式。生产零改动接入可以把 poll 作为正式路径：后端项目和 Alertmanager 不需要新增 webhook receiver，但 agentp 必须获得 Alertmanager 只读 API 访问。

### 10.1 两种模式

| 模式 | 配置 | 适用场景 |
|------|------|---------|
| **Push (webhook)** | `ALERT_SOURCE=webhook` | Alertmanager 可配置 webhook，实时性最高 |
| **Pull (poll)** | `ALERT_SOURCE=poll` | 后端项目/Alertmanager 不改配置的生产接入路径；agentp 主动读取 active alerts |
| **Both** | `ALERT_SOURCE=both` | webhook 实时 + poll 兜底（Alertmanager 重启丢消息时补齐） |
| **None** | `ALERT_SOURCE=none` | 仅手动发告警（测试用） |

### 10.2 Pull 实现

Celery Beat 周期任务，调用 Alertmanager `GET /api/v2/alerts`。生产实现必须把 poll 当成一个受控入口，而不是简单循环拉全量告警：

```python
# apps/worker/tasks.py

@celery_app.task(bind=True)
def poll_alertmanager(self) -> dict:
    """Pull active alerts from Alertmanager. No webhook config needed on their side."""
    settings = get_settings()

    if settings.alert_source not in ("poll", "both"):
        return {"status": "skipped", "reason": "alert_source_not_poll"}

    # 生产模式下读取 published effective config，优先于 raw settings
    effective = load_published_effective_config(db, settings.environment)
    endpoint = (effective.alertmanager_url if effective else None) or settings.alertmanager_url

    if not endpoint:
        record_poll_metric("disabled_total", reason="alertmanager_url_unresolved")
        audit_log(
            action="alert_poll.disabled",
            reason="alertmanager_url_unresolved",
        )
        return {"status": "disabled", "reason": "alertmanager_url_unresolved"}

    endpoint = endpoint.rstrip("/")

    filters = AlertPollFilters.from_settings(settings)

    if settings.app_env == "production" and not filters.has_valid_scope():
        record_poll_metric("disabled_total", reason="missing_valid_poll_scope")
        audit_log(
            action="alert_poll.disabled",
            reason="missing_valid_poll_scope",
            metadata=filters.audit_safe_dict(),
        )
        return {"status": "disabled", "reason": "missing_valid_poll_scope"}

    lock_key = f"alert-poll:{stable_hash(endpoint + str(filters.audit_safe_dict()))}"

    # 分布式锁：context manager 自动释放；Celery 崩溃时依赖 TTL 自动过期
    with redis_lock(lock_key, ttl=settings.alert_poll_lock_ttl_seconds) as acquired:
        if not acquired:
            return {"status": "skipped", "reason": "lock_not_acquired"}

        cursor = AlertPollCursorRepository(...).get_or_create(endpoint=endpoint, filters=filters)
        stats = PollStats()

        # namespace/service allowlist：必须在请求前转为服务端 filter[]，避免先拉全量
        if filters.namespace_allowlist or filters.service_allowlist:
            # effective 可能为 None（首次部署无 published config），回退到 settings 兜底
            label_mapping = (
                effective.service_label_mapping
                if effective and effective.service_label_mapping
                else settings.alert_poll_label_mapping
            )
            server_matchers = _allowlist_to_server_matchers(
                filters.namespace_allowlist, filters.service_allowlist,
                label_mapping=label_mapping,
            )
            if server_matchers:
                filters.matchers.extend(server_matchers)
            elif settings.app_env == "production":
                # 生产环境无法映射时在请求前拒绝，不先拉全量再判断
                record_poll_metric("disabled_total", reason="allowlist_cannot_map")
                return {"status": "disabled", "reason": "allowlist_cannot_map"}
            # 非生产环境：无法映射时退回到下面的客户端过滤

        try:
            alerts = AlertmanagerClient(
                base_url=endpoint,
                token=settings.alertmanager_read_token,
                timeout=settings.alert_poll_timeout_seconds,
            ).list_alerts(
                active=True,
                silenced=False,
                inhibited=False,
                unprocessed=False,
                receiver=filters.receiver_filter,
                filters=filters.to_alertmanager_matchers(),
            )
        except AlertmanagerError as exc:
            cursor.mark_degraded(error=str(exc), at=utcnow())
            record_poll_metric("failed_total", reason=exc.reason)
            audit_poll_failure(endpoint=endpoint, filters=filters, error=exc)
            return {"status": "degraded", "reason": exc.reason}

        # 非生产环境：allowlist 未能映射到服务端 filter 时，降级为客户端过滤
        if (filters.namespace_allowlist or filters.service_allowlist) and not server_matchers:
            alerts = _apply_allowlist_filters(
                alerts, filters.namespace_allowlist, filters.service_allowlist,
            )

        # 客户端侧截断（Alertmanager v2 API 无原生 limit）
        truncated = False
        if len(alerts) > settings.alert_poll_max_alerts_per_round:
            stats.truncated_total = len(alerts) - settings.alert_poll_max_alerts_per_round
            alerts = alerts[:settings.alert_poll_max_alerts_per_round]
            truncated = True

        for alert in alerts:
            stats.polled_total += 1

            normalized = _from_alertmanager_single_alert(alert)
            fingerprint = normalized["fingerprint"]

            # already_seen_active 有副作用：更新 current_seen_fingerprints 和 last_seen_at，
            # 防止 resolved 推断误判为 missing
            if cursor.already_seen_active(fingerprint, alert):
                cursor.mark_seen(fingerprint, alert, incident_id=cursor.incident_id_for(fingerprint))
                stats.deduplicated_total += 1
                continue

            if stats.created_total >= settings.alert_poll_max_new_incidents_per_round:
                stats.rate_limited_total += 1
                continue

            if rate_limiter.exceeded(service=normalized["service"]):
                stats.rate_limited_total += 1
                continue

            try:
                result = AlertService(...).create_alert(
                    source="alertmanager_poll",
                    fingerprint=fingerprint,
                    payload=normalized,
                )
            except Exception as exc:
                stats.failed_total += 1
                audit_poll_alert_failure(fingerprint=fingerprint, error=exc)
                continue

            if result.deduplicated:
                stats.deduplicated_total += 1
            else:
                stats.created_total += 1

            cursor.mark_seen(fingerprint, alert, incident_id=result.incident_id)

        # 基于 active fingerprint 缺失推断 resolved。
        # 如果本轮发生截断，禁止执行 resolved inference——未处理的 alert 仍在活跃，
        # 不能因为没进入本轮 set 而被误判为 resolved。
        if not truncated:
            infer_resolved_from_missing_fingerprints(cursor)
        else:
            record_poll_metric("resolved_inference_skipped", reason="truncated_round")

        cursor.save(last_polled_at=utcnow(), stats=stats)
        record_poll_metrics(stats)
        return stats.model_dump()
```

```python
# apps/api/schemas/alerts.py 中新增

def _from_alertmanager_single_alert(alert: dict) -> dict:
    """Parse a single alert from GET /api/v2/alerts (pull mode).

    The /api/v2/alerts response returns each alert as a flat dict where
    fingerprint, labels, annotations are all at the top level, unlike
    the webhook format which nests them under commonLabels + alerts[].
    """
    labels = {**_dict(alert.get("labels"))}
    annotations = {**_dict(alert.get("annotations"))}
    alert_name = _string(labels.get("alertname") or annotations.get("summary"), "AlertmanagerAlert")
    service = _string(labels.get("service") or labels.get("job") or labels.get("app"), "unknown")
    starts_at = _starts_at(alert.get("startsAt"))
    ends_at = _ends_at(alert.get("endsAt"))
    generator_url = _string(alert.get("generatorURL"), "")
    fingerprint = _string(
        alert.get("fingerprint"),
        "alertmanager:" + stable_hash(f"{service}:{alert_name}:{starts_at}:{generator_url}"),
    )

    return {
        "source": "alertmanager",
        "fingerprint": fingerprint,
        "service": service,
        "severity": _normalize_severity(labels.get("severity")),
        "alert_name": alert_name,
        "starts_at": starts_at,
        "ends_at": ends_at,
        "labels": labels,
        "annotations": annotations,
    }
```

### 10.3 Celery Beat 调度

```python
# apps/worker/celery_app.py 中配置 beat schedule

from celery.schedules import crontab

celery_app.conf.beat_schedule = {
    "poll-alertmanager": {
        "task": "apps.worker.tasks.poll_alertmanager",
        "schedule": 30.0,  # 每 30 秒，由 settings.alert_poll_interval_seconds 覆盖
        "options": {"expires": 60},  # 与 lock TTL 一致，防止任务堆积
    },
}
```

`expires` 只能减少排队堆积，不能替代分布式锁。生产实现必须以 Redis/DB lock 作为互斥来源。

### 10.4 配置

```bash
# .env
ALERT_SOURCE=poll                          # webhook | poll | both | none
ALERTMANAGER_URL=http://alertmanager-main.monitoring.svc:9093
ALERTMANAGER_READ_TOKEN=...                # 或使用 mTLS / in-cluster read-only ServiceAccount
ALERT_POLL_INTERVAL_SECONDS=30
ALERT_POLL_LOCK_TTL_SECONDS=60             # 必须 >= poll_timeout + processing_budget + safety_margin
ALERT_POLL_TIMEOUT_SECONDS=20              # 单次 Alertmanager API 调用超时
ALERT_POLL_RESOLVED_GRACE_PERIOD_SECONDS=120  # active fingerprint 缺失超过此时间才标记 resolved
ALERT_POLL_RESOLVED_MISSING_ROUNDS=3       # 连续缺失 N 轮后推断 resolved

# 生产必须至少配置一个有效范围过滤键：receiver / filter_matchers / namespace / service。
# severity 只能作为附加过滤条件，不允许作为唯一生产 poll 范围控制。
# 例如 severity=critical 不算有效 scope filter；
# 无有效范围过滤时仅 Alert Poll task disabled，不阻塞 worker 其他任务。
ALERT_POLL_RECEIVER_FILTER='sre|platform'
ALERT_POLL_FILTER_MATCHERS='severity=~"critical|warning",namespace=~"prod|payments"'
# filter matchers 会被解析为 Alertmanager filter[] 参数:
#   filter=severity=~"critical|warning"
#   filter=namespace=~"prod|payments"
# 注：上面的 severity 只是 namespace 的附加过滤，不能单独使用
ALERT_POLL_NAMESPACE_ALLOWLIST=prod,payments
ALERT_POLL_SERVICE_ALLOWLIST=checkout,payment

ALERT_POLL_MAX_ALERTS_PER_ROUND=200
ALERT_POLL_MAX_NEW_INCIDENTS_PER_ROUND=20
ALERT_POLL_MAX_INCIDENTS_PER_SERVICE_PER_MINUTE=5
```

`ALERTMANAGER_URL` 可以被 K8s 基础设施发现自动填充（见 3.7），进一步减少手动配置；但生产范围过滤、只读凭据和速率限制仍需要 operator 显式确认。

### 10.5 与 Webhook 的去重

两种模式共用 `AlertService.create_alert()`，其内置的 fingerprint 去重逻辑确保：

- Webhook 先到 → 创建 incident → Pull 再拉到同一条 → deduplicated
- Pull 先拉到 → 创建 incident → Webhook 再到 → deduplicated
- Alertmanager 重启、webhook 丢消息 → Pull 补上


### 10.6 Poll 生产安全要求

当目标是”不改 Alertmanager 配置”时，`ALERT_SOURCE=poll` 是正式生产接入路径，但必须补齐 webhook receiver 原本承担的范围控制和可靠性要求：

- 只读访问：使用只读 token、mTLS 或内网 ServiceAccount 访问 Alertmanager API。
- 分布式锁：每个 Alertmanager endpoint + receiver/filter 组合同一时刻只能有一个 poll task 工作，防止多 beat 重复创建 incident。锁 TTL 必须 >= poll_timeout + processing_budget + safety_margin（建议 60s，大于 poll interval 30s）。
- 范围过滤（硬约束）：生产环境下，`ALERT_SOURCE=poll` 或 `both` 时，必须至少配置一个有效范围过滤条件。有效范围过滤键为 `receiver`、`filter_matchers`（不含 severity-only matcher）、`namespace`、`service`。`severity` 只能作为附加过滤，**不允许作为唯一生产范围控制**。例如 `severity=critical` 不算有效 scope filter —— 无有效范围过滤时生产 poll 进入 disabled 状态。

```python
class AlertMatcher(BaseModel):
    """单个 Alertmanager matcher。结构化解析，不使用字符串 startswith 判断。"""
    label: str
    operator: Literal["=", "!=", "=~", "!~"]
    value: str

class AlertPollFilters(BaseModel):
    receiver_filter: str | None = None
    matchers: list[AlertMatcher] = Field(default_factory=list)  # 结构化 matcher 列表
    namespace_allowlist: list[str] = Field(default_factory=list)
    service_allowlist: list[str] = Field(default_factory=list)

    def has_valid_scope(self) -> bool:
        """severity-only matchers are NOT counted as a valid scope filter.

        只要 matchers 中存在 label != "severity" 的 matcher，就算有有效 matcher scope。
        """
        return any([
            bool(self.receiver_filter),
            any(m.label.strip().lower() != "severity" for m in self.matchers),
            bool(self.namespace_allowlist),
            bool(self.service_allowlist),
        ])

    def to_alertmanager_matchers(self) -> list[str]:
        """转换为 Alertmanager HTTP API filter[] 参数列表。value 中的特殊字符会被转义。"""
        return [
            f'{m.label}{m.operator}"{_escape_matcher_value(m.value)}"'
            for m in self.matchers
        ]


    def audit_safe_dict(self) -> dict:
        """返回可写入 audit log 的脱敏摘要。"""
        return {
            "receiver_filter": self.receiver_filter,
            "matchers": [m.model_dump() for m in self.matchers],
            "namespace_allowlist": self.namespace_allowlist,
            "service_allowlist": self.service_allowlist,
        }


def _escape_matcher_value(value: str) -> str:
    return (
        value
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
    )
```

#### Matcher Parser

`ALERT_POLL_FILTER_MATCHERS` 支持逗号分隔的 Alertmanager matcher 表达式。解析规则：

1. 支持 `=`、`!=`、`=~`、`!~` 四种操作符。
2. value 可以带双引号，也可以不带；解析后统一保存为 `AlertMatcher`。
3. 逗号只作为 matcher 分隔符；引号内的逗号不得拆分。
4. label 必须匹配 `[a-zA-Z_][a-zA-Z0-9_]*`。
5. 正则 matcher（`=~` / `!~`）必须能被编译，否则配置无效。
6. severity-only 不算有效 poll scope。

有效示例：

```bash
# namespace 是 non-severity matcher → 有效 scope
export ALERT_POLL_FILTER_MATCHERS='severity=~"critical|warning",namespace=~"prod|payments"'
```

无效示例：

```bash
# 只有 severity matcher → 无效 scope，poll 会被 disabled
export ALERT_POLL_FILTER_MATCHERS='severity=~"critical|warning"'
```

#### 生产 poll disabled 行为

生产环境下，`ALERT_SOURCE=poll` 或 `both` 时，如果没有有效 poll scope，**只禁用 Alert Poll task，不阻塞 worker 其他任务启动**。

有效 scope 包括：`receiver_filter`、non-severity matcher、`namespace_allowlist`、`service_allowlist`。

禁用后行为：
- 不执行 Alertmanager 拉取；
- 写入 audit log 和 metric（`disabled_total`）；
- discovery、diagnosis、runbook、approval 等 worker 任务继续可用；
- `/api/discovery/status` 或 `/api/alerts/poll/status` 显示 disabled reason。

实现上可对应 `ALERT_POLL_RECEIVER_FILTER`、`ALERT_POLL_FILTER_MATCHERS`、`ALERT_POLL_NAMESPACE_ALLOWLIST`、`ALERT_POLL_SERVICE_ALLOWLIST`。
- 去重键：优先使用 Alertmanager fingerprint；缺失时使用 `service + alertname + startsAt + generatorURL` 派生稳定 fingerprint。
- 速率限制：单轮最大拉取数、单轮最大新建 incident 数、每 service 每分钟上限。
- 状态游标（cursor）：保存以下字段用于审计、重启恢复和 resolved 推断：
  - `active_fingerprint_set`：当前 active 的 fingerprint 集合
  - `last_seen_at`：每个 fingerprint 的最后出现时间
  - `missing_since`：fingerprint 首次未出现的时间（用于 resolved 推断）
  - `last_polled_at`：最近一次 poll 完成时间
  - `incident_id`：fingerprint 对应的 incident ID
- **Resolved 推断机制**：Alertmanager 的 `/api/v2/alerts` 只返回当前 active alerts，不包含 resolved。因此采用方案 A——基于 active fingerprint 缺失推断 resolved：
  - 每轮 poll 后对比当前 active fingerprint set 与 cursor 中保存的 `active_fingerprint_set`
  - 如果某个 fingerprint 连续 `RESOLVED_MISSING_ROUNDS`（默认 3）轮未出现
  - 且 `missing_since` 距今超过 `RESOLVED_GRACE_PERIOD_SECONDS`（默认 120s）
  - 则将对应 incident 标记为 resolved，不重复触发诊断
  - 这样无需额外查询 Alertmanager resolved alerts API，也无需配置 webhook

> **重要**：Poll 模式下的 resolved 是 agentp 基于 active alerts 连续缺失推断出来的本地状态，不等价于 Alertmanager webhook `send_resolved` 事件。如果生产环境必须精确记录 resolved 时间，应优先使用 `ALERT_SOURCE=webhook` 或 `both`。poll resolved 主要用于零改动接入和兜底补偿。
- 失败降级：Alertmanager 不可达时 poll 标记 degraded 并记录 metrics，不影响 webhook 或手动告警入口。
- 审计指标：记录 `polled_total`、`created_total`、`deduplicated_total`、`filtered_total`、`failed_total`、`resolved_total` 和 poll duration。

建议生产配置：

```bash
ALERT_SOURCE=poll
ALERTMANAGER_URL=http://alertmanager-main.monitoring.svc:9093
ALERTMANAGER_READ_TOKEN=...
ALERT_POLL_INTERVAL_SECONDS=30
ALERT_POLL_LOCK_TTL_SECONDS=60             # lock TTL >= poll_timeout + processing_budget + safety_margin
ALERT_POLL_TIMEOUT_SECONDS=20
ALERT_POLL_RESOLVED_GRACE_PERIOD_SECONDS=120
ALERT_POLL_RESOLVED_MISSING_ROUNDS=3
ALERT_POLL_RECEIVER_FILTER='sre|platform'
ALERT_POLL_FILTER_MATCHERS='severity=~"critical|warning",namespace=~"prod|payments"'
# filter matchers 会被解析为 Alertmanager filter[] 参数:
#   filter=severity=~"critical|warning"
#   filter=namespace=~"prod|payments"
# receiver / filter_matchers / namespace / service 至少配置一个；severity 建议只作为附加过滤
ALERT_POLL_MAX_ALERTS_PER_ROUND=200
ALERT_POLL_MAX_NEW_INCIDENTS_PER_ROUND=20
ALERT_POLL_MAX_INCIDENTS_PER_SERVICE_PER_MINUTE=5
```


---

## 11. 安全考虑

| 风险 | 措施 |
|------|------|
| Discovery 探测 K8s API 权限过大 | 只用 read-only API：list pods/deployments/services/namespaces |
| 后端 API 未认证访问 | 支持 bearer token、basic auth、mTLS、CA 证书、TLS verify；K8s 内优先使用只读 ServiceAccount |
| 认证凭据泄露进入 LLM prompt | token、密钥、证书路径不得进入 LLM context；redaction 必须在 LLM 调用前完成 |
| Discovery 查询对监控系统造成压力 | timeout、范围收敛与截断、缓存、结果数上限、namespace/service 白名单、采样（见 3.10） |
| 联网搜索泄露服务名 | 搜索查询只用故障类型通用描述 + 技术栈，不含服务真实名称 |
| 搜索结果含恶意链接 | 只抓文本，不执行 JS，不下载二进制；拒绝内网 IP、metadata endpoint、file 协议；生产默认关闭 web search |
| LLM 幻觉指标名 | MetricMatcher 结果优先于 LLM 推断；不在 discovery 列表中的指标名自动标记 needs_human_verification |
| 云端 LLM 可能接触敏感上下文 | 只发送压缩、脱敏后的必要证据；过滤 Secret、token、认证头、完整 env、完整 ConfigMap 和不必要 PII；记录 redaction audit；本地 LLM profile 可用于更严格数据驻留环境 |
| Prompt/响应审计泄露敏感数据 | prompt audit 只保存摘要、hash、evidence IDs、redaction 状态和 token 统计，不保存未脱敏原文 |
| Poll 模式重复或过量创建事故 | 分布式锁（TTL >= interval + safety_margin）、范围过滤（至少一个有效过滤键，severity-only 禁止）、速率限制、稳定 fingerprint、cursor 追踪、resolved 推断和审计指标；缺少有效范围过滤时仅 Alert Poll task disabled，不阻塞 worker 其他任务 |
| Runbook Amendment 引入错误知识 | 必须人工审批；基于 >= 3 次事故的统计规律，非单次偶然 |
| 旧 Discovery 配置长期生效 | EffectiveConfigVersion 过期策略（默认 30 天），过期后回退到显式 env/profile |
| 配置变更无据可查 | 所有配置发布、回滚、override 写入结构化审计日志（actor、action、target、before/after、reason、source） |

---

## 12. 新用户接入体验

### K8s 环境（supervised 自动化）

```bash
# 生产默认不启用 LLM，诊断使用确定性证据 + Runbook 检索。
# 如环境评审允许 LLM，取消注释以下行并选择一种 profile（deepseek/openai/anthropic/vllm）。
# export LLM_PROVIDER=deepseek          # 示例：云端 LLM
# export LLM_API_KEY=<provider-api-key>
# 或本地 LLM:
# export LLM_PROVIDER=vllm
# export LLM_BASE_URL=http://vllm:8000/v1

# Discovery 自动生成候选配置；高置信只读配置可自动发布，其余进入 review queue
# 后端项目零改动时使用 poll；无需改 Alertmanager receiver，但需要只读 API 权限和范围过滤
export AUTOMATION_LEVEL=supervised
export ALERT_SOURCE=poll
export ALERTMANAGER_URL=http://alertmanager-main.monitoring.svc:9093
export ALERTMANAGER_READ_TOKEN=<alertmanager-read-token>
export ALERT_POLL_LOCK_TTL_SECONDS=60
export ALERT_POLL_RECEIVER_FILTER='sre|platform'
export ALERT_POLL_FILTER_MATCHERS='severity=~"critical|warning",namespace=~"prod"'

# 启动（仅 agentp 核心服务）
docker compose up -d api worker web postgres redis

# worker 日志输出:
# [discovery] k8s backend: prometheus-k8s.monitoring.svc:9090
# [discovery] k8s backend: loki-gateway.monitoring.svc:3100
# [discovery] k8s backend: jaeger-query.observability.svc:16686
# [discovery] k8s backend: alertmanager-main.monitoring.svc:9093
# [discovery] 4/4 supported backends auto-discovered
# [discovery] detected_only backends: tempo (tempo.monitoring.svc), grafana (grafana.monitoring.svc)
# [discovery] detected service label: "app" (confidence: 0.95)
# [discovery] found 12 services: api-gateway, user-svc, ...
# [discovery] matched 8/8 metric types  # 或明确列出 unavailable metrics；无 Prometheus 时诊断写 hypothesis
# [discovery] topology derived: 9 nodes, 14 edges (confidence: 0.72)
# [alert] pull mode enabled, polling alertmanager every 30s
# [runbook] generated 5 drafts for user-svc (pending review)

# 进入 Web UI:
# 1. 查看 Discovery 状态 -> 确认自动检测结果
# 2. 审查 Runbook 草稿 -> 批准/编辑/拒绝
# 3. 一切就绪
```

### 非 K8s 环境（手动配置后端地址）

```bash
export PROMETHEUS_URL=http://prom:9090
export LOKI_URL=http://loki:3100
export JAEGER_URL=http://jaeger:16686
export ALERTMANAGER_URL=http://alertmanager:9093
export ALERTMANAGER_READ_TOKEN=<alertmanager-read-token>
export ALERT_SOURCE=poll             # 不改 Alertmanager 配置时使用 poll；可改 receiver 时也可使用 webhook/both
export ALERT_POLL_LOCK_TTL_SECONDS=60
export ALERT_POLL_FILTER_MATCHERS='severity=~"critical|warning",service=~"checkout|payment"'
# 如环境评审允许 LLM：
# export LLM_PROVIDER=deepseek
# export LLM_API_KEY=<provider-api-key>

docker compose up -d api worker web postgres redis

# 如果后续允许修改 Alertmanager，可切换 ALERT_SOURCE=webhook 或 both，并配置 webhook 指向 agentp；poll 路径本身不要求改 Alertmanager receiver。
```

---

## 13. 部署注意事项

### 13.1 精简服务栈

agentp 的 `docker-compose.yml` 默认启动了完整 demo 环境（Prometheus、Loki、Grafana、Jaeger、OTel Collector、BGE-ZH、Mailpit、demo-service）。**接入已有可观测性栈后，这些全部不需要**，只需要保留 agentp 自身的核心服务：

```
保留（必须）:

  ┌──────────┐   ┌──────────┐   ┌──────────┐   ┌──────────┐   ┌──────┐
  │   api    │   │  worker  │   │   web    │   │ postgres │   │ redis │
  │  :8000   │   │  (无端口) │   │  :5173   │   │  :5432   │   │ :6379 │
  └──────────┘   └──────────┘   └──────────┘   └──────────┘   └──────┘

可以移除（已有真实后端替代）:

  Prometheus  :9090     → 用 PROMETHEUS_URL 指向已有
  Loki        :3100     → 用 LOKI_URL 指向已有
  Grafana     :3000     → 用已有的 Grafana
  Jaeger      :16686    → 用 JAEGER_URL 指向已有
  OTel Col    :4317/8   → 用已有的 OTel Collector
  BGE-ZH      :8083     → 可选增强服务：
                            - 不启用 Runbook semantic search 时可移除；
                            - 使用外部 embedding provider 时可移除；
                            - 仅当使用本地向量检索且 Runbook RAG 依赖本地 embedding 时需要保留
  Mailpit     :8025     → 配置真实 SMTP 后移除
  demo-svc    :8080     → 故障注入 demo，不需要
  Promtail    (日志采集) → 已有 Loki + Promtail 栈
```

对应的 `docker-compose.yml` 只需要保留 5 个核心 service 定义（api、worker、web、postgres、redis），其余全部删掉或注释。如果不需要 Web UI，可以进一步缩减为 4 个（api、worker、postgres、redis）。

### 13.2 端口冲突处理

如果宿主机上已有服务占用了默认端口：

```yaml
# docker-compose.yml — 端口映射调整
services:
  api:
    ports:
      - "8001:8000"    # 宿主机 8001 -> 容器 8000

  web:
    ports:
      - "5174:5173"    # 宿主机 5174 -> 容器 5173

  postgres:
    ports:
      - "5433:5432"    # 避开宿主机已有的 PostgreSQL
```

对应的 `.env` 调整：

```bash
WEB_BASE_URL=http://localhost:5174       # 匹配 web 端口
DATABASE_URL=postgresql+psycopg://sre:sre@localhost:5433/sre  # 匹配 PG 端口
```

### 13.3 K8s 环境

如果 agentp 部署在 K8s 集群内（推荐），没有端口冲突问题——每个 Pod 有独立 IP。只需注意：

- K8s Discovery 需要 ServiceAccount 权限（read-only：list pods/deployments/services/namespaces）
- 可观测性后端通过 K8s Service DNS 访问：`http://prometheus-k8s.monitoring.svc:9090`
- PostgreSQL + Redis 可用 StatefulSet 或外部托管服务

### 13.4 Alertmanager Webhook 配置

```yaml
# alertmanager.yml
receivers:
  - name: 'sre-agent'
    webhook_configs:
      - url: 'http://agentp-api:8000/api/alerts'
        send_resolved: true
```

Alertmanager 发的标准 JSON payload 会被 `_from_alertmanager()` 自动解析。无需格式转换。

---

## 14. 实施路线

### Phase 0: Production Safety Foundation (2-3 天)
- `AutomationDecision` / `AutomationPolicy`，统一 `auto_apply`、`require_review`、`reject`、`record_only` 语义
- `EffectiveConfigVersion`、`DiscoveryRun`、`DiscoveryOverride` 数据模型和迁移
- production 默认值：`LLM_PROVIDER=disabled`、`EXECUTOR_BACKEND=fixture`、Runbook LLM/web search 默认关闭
- 生产环境禁止默认 localhost 后端参与诊断
- Alert poll 安全策略基础：分布式锁、范围过滤（至少一个有效过滤键：receiver / non-severity matcher / namespace / service；severity-only 禁止）、速率限制
- 审计：配置发布、override、自动发布、拒绝发布都写 audit log（`AuditLogEntry` 结构）
- 单元测试：未发布 discovery 不进入 worker；override 优先级；回滚配置版本

### Phase 1: Core Discovery (3-4 天)
- `DiscoveryResult`、`MetricMapping`（含 `status` / `unavailable` 支持）、`LabelConvention` 等数据模型
- `PromDiscovery` + `MetricMatcher`（含 label 要求、`/series` 验证、PromQL dry-run）
- `ConfigMerge` 生成 proposal，不直接修改 runtime settings
- 单元测试（mock Prometheus API 响应、指标缺失、低置信度不自动发布、unavailable 标记正确）

### Phase 2: K8s & Topology (2-3 天)
- `K8sDiscovery`（服务列表 + label 检测，metric-level `/series` 交叉验证）
- `TopologyDeriver`：拆分 `WorkloadBinding`（Service selector）和 `ServiceEdge`（manual topology / trace call graph / env var / configmap）
- `LokiDiscovery`（label 交叉验证）
- 单元测试

### Phase 3: Runner & Degradation (2-3 天)
- `DiscoveryRunner` 编排 + 并行执行，作为独立 Celery task 运行
- 逐组件降级逻辑，指标缺失时输出 capability gaps / degraded signals / used_fallback_signals
- Discovery 成本控制：timeout、缓存、结果数上限、namespace/service 白名单、采样
- Discovery 结果持久化到 DB，生产 worker 只读取 published effective config version
- 集成测试（多环境 fixture 模拟、无业务指标、低置信 label、后端不可达）

### Phase 4: Runbook Template Generation (2-3 天)
- `RunbookTemplateEngine`（Jinja2 渲染，确定性生成）
- 能力矩阵驱动模板段落可见性
- 审查 API（draft CRUD + review decision；未审核 draft 不进入 `runbook_chunks`）
- BGE-ZH 可选化：不启用 Runbook semantic search 或使用外部 embedding provider 时可移除

### Phase 5: Deterministic Runbook Feedback (2-3 天)
- 聚合同类 incident
- 统计成功/失败/跳过/拒绝的动作
- 识别 Runbook 中缺失的 fault type、service、diagnostic step
- 生成 `RunbookFeedbackSummary` + `AmendmentDraft` 框架
- **不调用 LLM，不调用 web_search**
- 所有结果进入 review queue，不直接写入 `runbook_chunks`

### Phase 6: Alert Pull Production Hardening (2-3 天)
- Alertmanager poll cursor（`active_fingerprint_set`、`last_seen_at`、`missing_since`、`last_polled_at`）
- Resolved 推断（连续 `RESOLVED_MISSING_ROUNDS` 轮缺失 + grace period）
- 分布式锁（TTL >= interval + safety_margin，建议 60s）
- `ALERT_POLL_FILTER_MATCHERS` matcher 解析与 Alertmanager `filter[]` 转换
- 速率限制、审计指标（`polled_total`、`created_total`、`deduplicated_total`、`filtered_total`、`failed_total`、`resolved_total`）

### Phase 7: API, Integration & Deployment (2-3 天)
- Discovery API（status/services/metrics/topology/capabilities/rerun/override）
- Effective config publish/revoke/rollback API + 配置版本过期策略
- 与 `_build_deps()` 集成：生产只注入 published config + 显式 env/profile
- 后端认证配置（bearer token、basic auth、mTLS、CA 证书、TLS verify）
- 精简 docker-compose：核心 5 个 service（api、worker、web、postgres、redis）
- 前端 Discovery 状态面板、配置发布/回滚面板、Runbook 审查界面
- 生产接入文档

### Phase 8: Testing & Docs (2-3 天)

只包含测试、文档和 E2E 验证：
- 多环境集成测试
- 生产安全测试：无指标降级、poll 锁、poll 范围过滤、severity-only 禁止、resolved 推断、override audit、未发布配置隔离、云端 LLM redaction、web search 默认关闭、L2/L3/L4 guardrail 边界
- 文档更新
- 端到端验证

Phase 8 不实现任何新功能：不含 LLM 差异分析、不含 web_search 验证、不含 LLM Runbook Generation、不含 TempoTraceBackend、不含 Grafana webhook parser。

### Phase 9+: Advanced Extensions (后续)

**LLM Runbook Generation + Web Search:**
- `RunbookWebSearcher`（联网搜索 + 内容提取；生产默认关闭）
- `LLMRunbookGenerator`（LLM + Web Search Tool Use；生产默认关闭）
- SSRF 防护、搜索脱敏、来源追溯、搜索缓存、成本控制
- 只能生成 `RunbookDraft`，不能直接进入 `runbook_chunks`

**LLM 差异分析与 Amendment 自动草稿:**
- LLM 对比现有 Runbook 与历史 incident
- LLM 生成 proposed amendment text
- web_search 验证外部资料
- 带 citations 和 search_trail 的 `RunbookAmendment` draft

**Tempo Trace Backend:**
- `TempoTraceBackend` 实现
- `TRACE_BACKEND=tempo`、`TEMPO_URL`、`tempo_auth`
- Tempo 自动发现结果启用

**Grafana Webhook:**
- `_from_grafana_alert()` parser
- Grafana alert payload（rule UID、folder、orgId、dashboard URL、panel URL）
- 与 `AlertService.create_alert()` fingerprint 去重模型对齐

**增强 Trace Call Graph（Phase 9+）:**
- 在 Phase 2 基础 Jaeger trace call graph 之上增强：跨时间窗口聚合、多 trace 置信度计算、Tempo 支持、调用方向冲突处理、边权重统计

---

**总计约 19-25 天（Phase 0-8 核心交付）**。Phase 0-3 可独立交付（解决生产安全适配与指标缺失降级的核心痛点），Phase 4-7 增强 Runbook 基础能力和 Alert poll 生产可靠性，Phase 9+ 作为后续高级能力。
