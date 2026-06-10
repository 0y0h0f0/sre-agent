# Real Backend Integration Design

**Date:** 2026-06-10
**Status:** draft
**Scope:** agentp 接入真实 Prometheus + Loki + Jaeger + Kubernetes 后端，并实现插即用适配

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
| LLM | `FakeLLM` | `OpenAIAdapter` / `DeepSeekAdapter` / `AnthropicAdapter` | `LLM_PROVIDER=openai/deepseek/anthropic` |

**问题**：虽然代码支持真实后端，但每次接入新环境需要手动适配标签约定、指标命名和拓扑关系。例如 Prometheus 中区分服务的 label 可能是 `app`、`service`、`job`、`app.kubernetes.io/name` 等，指标名也因采集方案（Istio、kube-prometheus-stack、OpenTelemetry）而异。

### 1.2 目标

将 agentp 打造为**对满足 Prometheus + Loki + Jaeger + K8s 标准栈的后端项目可快速适配**的系统。新用户接入只需配置 4 个 URL，其余由系统自动探测完成。

### 1.3 非目标

- 不改变现有 Backend Protocol 抽象
- 不改变安全边界（mock executor only, L4 hard reject, L3 二次确认）
- 不改变 CI 稳定性要求（fixture 默认保持确定性测试）
- 不支持非 K8s 环境的自动发现（可通过 profile 手动适配）

---

## 2. 总体架构

```
                        ┌──────────────┐
                        │ Alertmanager  │
                        │ / Grafana     │
                        └──────┬───────┘
                               │ webhook POST /api/alerts
                               ▼
┌──────────────────────────────────────────────────────────────┐
│                       agentp                                 │
│                                                              │
│  ┌────────────────────┐   ┌──────────────────────────────┐   │
│  │  API (FastAPI)      │   │  Worker (Celery)             │   │
│  │  - POST /alerts     │   │                              │   │
│  │  - GET /discovery/* │   │  启动时:                      │   │
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
│           │               │  │ RunbookWebSearcher      │  │   │
│           │               │  │ LLMRunbookGenerator     │  │   │
│           │               │  │ RunbookFeedbackAnalyzer │  │   │
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
├── topology.py         # K8s Service -> 依赖图推导
├── runner.py           # 编排所有 discovery，合并结果
├── config_merge.py     # 多来源配置优先级合并
└── cache.py            # 结果持久化（JSON），支持复用
```

### 3.2 启动数据流

```
worker 启动
    │
    ▼
DiscoveryRunner.run()
    │
    ├──(1) K8sDiscovery
    │     ├── GET /api/v1/namespaces  -> namespace 列表
    │     ├── GET /api/v1/pods        -> 采样 Pod labels，统计 label key 分布
    │     ├── GET /apis/apps/v1/deployments -> 服务列表
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
          └── 从 K8s Services + Pods 推导依赖图
              │
              ▼
         DiscoveryResult (merged, validated)
              │
              ▼
         写入缓存文件 -> ConfigMerge -> 构造 Tool 实例
```

### 3.3 核心数据模型

```python
# packages/discovery/models.py

class DiscoveredService(BaseModel):
    name: str
    namespace: str
    kind: str                    # Deployment | StatefulSet | DaemonSet
    replicas: int | None = None
    labels: dict[str, str] = {}
    ports: list[int] = []

class MetricMapping(BaseModel):
    semantic_type: str           # latency | error_rate | qps | cpu | ...
    prometheus_metric: str       # 实际的 Prometheus 指标名
    promql_template: str         # 参数化的 PromQL
    confidence: float            # 匹配置信度 0-1
    source: str                  # "auto" | "manual"

class LabelConvention(BaseModel):
    metrics_service_label: str
    logs_service_label: str
    confidence: float
    alternatives: list[dict] = []  # [{key, coverage}]

class ServiceEdge(BaseModel):
    source: str
    target: str
    evidence: str                # "k8s_env_var" | "configmap" | "service_selector" | "manual"

class ServiceTopology(BaseModel):
    services: list[str]
    edges: list[ServiceEdge]

class EnvironmentCapability(BaseModel):
    has_metrics: bool = True
    has_logs: bool = True
    has_traces: bool = True
    has_k8s: bool = False
    has_db_diagnostics: bool = False
    has_deployment_tracking: bool = False

class DiscoveryResult(BaseModel):
    run_at: datetime
    label_convention: LabelConvention
    services: list[DiscoveredService]
    metric_mappings: list[MetricMapping]
    topology: ServiceTopology
    capabilities: EnvironmentCapability
    primary_namespace: str
    warnings: list[str] = []
    recommendations: list[str] = []
```

### 3.4 指标语义匹配引擎

内置语义模板库，每个语义类型对应一组候选正则模式：

```python
SEMANTIC_PATTERNS: dict[str, list[str]] = {
    "latency": [
        r".*request.*duration.*bucket",     # Istio/OpenTelemetry
        r".*http.*duration.*bucket",        # kube-prometheus-stack
        r".*latency.*bucket",               # 自定义
        r".*response_time.*bucket",         # 旧式
    ],
    "error_rate": [
        r".*requests_total.*",              # 需配 status=~"5.."
        r".*http_errors.*",
        r".*failed_requests.*",
    ],
    "qps": [
        r".*requests_total.*",
        r".*throughput.*",
        r".*rpc.*total.*",
    ],
    "cpu_throttle": [
        r".*cpu.*cfs_throttled.*",
        r".*cpu.*throttling.*",
    ],
    "disk_avail": [
        r".*filesystem.*avail.*",
        r".*disk.*free.*",
    ],
    # ... 更多语义类型
}
```

匹配算法：

```
对于每个语义类型:
  1. 用候选正则逐一匹配 available_metrics
  2. 按正则声明顺序作为优先级
  3. 对最佳匹配，调用 Prometheus query_range 确认有数据
  4. 提取该指标的 label set 确认 service_label 存在
  5. 生成参数化 PromQL
  6. 第一候选无数据 -> fallback 到第二候选
```

### 3.5 Service Label 检测

```python
class K8sDiscovery:
    async def detect_service_label(self) -> LabelConvention:
        # 1. 获取所有 Pod 的 label keys
        # 2. 统计每个 label key 的出现频率和覆盖率
        # 3. 候选: app, app.kubernetes.io/name, service, job,
        #          component, deployment, k8s-app, name
        # 4. 选覆盖率 >= 80% 的最高频 key
        # 5. 无达标 -> 取最高值 + low confidence + warn
        # 6. 交叉验证: Prometheus /api/v1/labels 确认该 label 存在
```

### 3.6 拓扑推导

三种策略，按优先级 fallback：

1. **环境变量注入推导（推荐）**：扫描 Deployment spec 的 env，匹配 DNS 模式 `<svc>.<ns>.svc.cluster.local` 和 `*_SERVICE_HOST` 约定
2. **K8s Service selector 匹配**：Service 的 selector 指向的 Pod -> Pod 的 ownerRef -> 对应的 Deployment/StatefulSet
3. **手动拓扑文件**：`SERVICE_TOPOLOGY_PATH` 指向 JSON（兜底）

### 3.7 Backend Infrastructure Auto-Discovery

除了发现被诊断的服务，DiscoveryRunner 也自动定位可观测性基础设施。在 K8s 环境下，无需手动配置 Prometheus / Loki / Jaeger / Alertmanager 的地址。

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
    │       Pattern                   → 后端类型
    │       ─────────────────────────────────────────
    │       prometheus-*              → Prometheus
    │       alertmanager-*            → Alertmanager
    │       loki-*                    → Loki
    │       jaeger-query*             → Jaeger Query
    │       tempo-*                   → Tempo
    │       grafana-*                 → Grafana
    │
    ├── 3. 对每个候选 service，探测 health endpoint 确认身份:
    │
    │       Prometheus:   GET /-/healthy                     → 200
    │       Loki:         GET /ready                         → 200
    │       Jaeger:       GET /api/services                  → 200
    │       Alertmanager: GET /api/v2/status                 → 200
    │       Tempo:        GET /api/search?tags=service.name  → 200
    │       Grafana:      GET /api/health                    → 200
    │
    └── 4. 确认 → 自动填充 BackendEndpoints
```

#### 数据模型

```python
class BackendEndpoints(BaseModel):
    """自动发现的可观测性基础设施地址"""
    prometheus_url: str | None = None
    loki_url: str | None = None
    jaeger_url: str | None = None
    alertmanager_url: str | None = None
    grafana_url: str | None = None

    # 发现质量
    auto_discovered: bool = False       # 全部来自自动发现
    discovered_count: int = 0
    total_count: int = 5                # prometheus + loki + jaeger + alertmanager + grafana
    missing: list[str] = []             # 未找到的后端列表

class DiscoveryResult(BaseModel):
    # ... 原有字段 ...
    backends: BackendEndpoints          # 新增
```

#### 配置优先级

自动发现的地址优先级低于手动配置——用户显式设置 `.env` 中的 `PROMETHEUS_URL` 优先于 K8s 发现结果：

```python
def _resolve_backend(user_value, discovered_value, endpoint_type):
    if user_value and user_value != defaults.get(endpoint_type):
        return user_value          # 用户显式配置，最高优先级
    if discovered_value:
        return discovered_value    # K8s 自动发现
    return defaults.get(endpoint_type)  # 内置默认值 (localhost)
```

#### 降级：非 K8s 或权限不足

```
K8s API 不可达
    │
    └── backends 全部标记 None，missing = ["prometheus", "loki", "jaeger", "alertmanager", "grafana"]
        │
        ├── 如果用户配置了 PROMETHEUS_URL 等 → 正常启动
        │
        └── 如果用户也没配 → warnings 追加:
            "Run outside K8s and no backend URLs configured — agent will start
             but tool calls will fail. Set PROMETHEUS_URL, LOKI_URL, JAEGER_URL
             in .env or ensure K8s RBAC access."
```

---

## 4. 降级策略

设计原则：**逐级降级，不整体失败**。Discovery 的局部失败不阻塞 agent 启动。

### 4.1 逐组件降级

| 组件 | 失败原因 | 降级行为 |
|------|---------|---------|
| K8s API | 不在 K8s 内 / RBAC 不足 | services 列表为空，label 回退到 Prometheus 探测，topology 回退到静态文件 |
| Prometheus | 网络不通 / 认证失败 | metric_mappings 全部标记 unavailable，Tool 层回退到 settings 默认值 |
| 单个指标 | 该环境不采集此指标 | 该语义类型标记 confidence=0，证据交叉验证自动降权 |
| Label 模糊 | 无 key 覆盖率 > 80% | 取最高覆盖率候选，confidence 低于阈值时标记需人工确认 |
| Loki | 不可达 | logs_service_label 沿用 metrics label，日志节点返回 degraded |
| 拓扑推导 | 无 env var / 无 Service selector | topology 为空，diagnose 跳过级联分析，不影响单服务诊断 |
| 基础设施发现 | 不在 K8s 内 / RBAC 不足 | backends 全部标记 missing，回退到 `.env` 用户配置或默认值，agent 仍可启动 |
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

---

## 5. 配置优先级系统

```
优先级（高->低）:

1. 环境变量 (.env)           <- 用户显式设定，最高权威
2. Profile 文件              <- 用户选择的预设 profile
3. Discovery 缓存            <- 自动探测（confidence >= 阈值时采纳）
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

**置信度阈值**：`>= 0.8` 自动采纳；`0.5-0.8` 采纳但 warn；`< 0.5` 回退默认值。

---

## 6. Runbook 生成

### 6.1 三层生成策略

```
DiscoveryResult (服务名、指标映射、能力矩阵、拓扑)
        │
        ├──> 第一层: 模板填充 (确定性, 100% 可用)
        │       Jinja2 模板 + discovery 变量 -> 基础 Runbook 骨架
        │       标记 source=template, confidence=medium
        │
        └──> 第二层: LLM + Tool Use 生成 (可选, RUNBOOK_LLM_GENERATION_ENABLED)
                LLM 拥有 web_search 工具，可在生成过程中主动搜索
                循环: 生成段落 -> 发现缺口 -> 搜索 -> 补充 -> 继续
                输出: RunbookDraft (正文 + SelfCritique + 完整搜索追溯)
```

**关键变化**：联网搜索不再是独立的预处理步骤，而是 LLM 手中的一个 Tool。LLM 在生成 Runbook 时，遇到不确定的细节（如某个数据库版本的具体参数、某个中间件的已知 bug）可以主动发起搜索，拿到结果后继续写。这比"提前搜好喂给 LLM"更精准——LLM 知道自己的知识缺口在哪里。

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
    citations: list[Citation] = []     # 引用的 URL

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
    search_trail: list[SearchTrailEntry] = []  # 完整搜索链路
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

### 6.5 审查 API

```
GET  /api/runbooks/drafts?service={name}
     -> 待审查草稿列表（按 confidence 排序，低置信度优先）

GET  /api/runbooks/drafts/{draft_id}
     -> 单个草稿完整内容 + 自评 + 信息源追溯

POST /api/runbooks/drafts/{draft_id}/review
     -> {"decision": "approve" | "edit" | "reject", "edited_content": "...", "comment": "..."}

POST /api/runbooks/regenerate
     -> {"service": "...", "fault_types": ["..."]}
     -> 重新触发搜索+LLM生成
```

---

## 7. Runbook Feedback Loop

### 7.1 流程

```
Incident 完成
      │
      ├──> persist_memory (现有, 不改)
      │      └── L0-L3 Memory
      │
      └──> RunbookFeedbackAnalyzer (新增)
              │
              ├── 条件触发:
              │     ├── 同类故障累计 >= 3 次
              │     ├── 根因置信度 >= 0.7
              │     └── 有实际成功执行的动作
              │
              ├── LLM 差异分析 (带 web_search tool):
              │     ├── 对比本次诊断 vs 现有 Runbook
              │     ├── 识别缺失/过时内容
              │     ├── 可主动搜索外部资料验证修正建议
              │     └── 生成 RunbookAmendment
              │
              └── -> 人工审批 -> 写入 runbook_chunks 表
```

### 7.2 Amendment 数据结构

```python
class AmendmentItem(BaseModel):
    section: str                      # Symptoms / Root Causes / Diagnostic Steps / Remediation
    original_text: str | None = None  # 修改场景下的原文
    proposed_text: str                # 建议文本
    evidence_from_incidents: list[str]
    priority: Literal["critical", "high", "medium", "low"]

class RunbookAmendment(BaseModel):
    id: str
    service: str
    fault_type: str
    proposed_at: datetime
    additions: list[AmendmentItem] = []
    modifications: list[AmendmentItem] = []
    deprecations: list[AmendmentItem] = []
    based_on_incidents: list[str] = []
    confidence: float
    rationale: str
    status: Literal["pending", "approved", "rejected", "applied"] = "pending"
    reviewed_by: str | None = None
    applied_at: datetime | None = None
```

### 7.3 频率控制

```bash
RUNBOOK_AMENDMENT_COOLDOWN_DAYS = 7     # 同 fault_type 至少间隔 7 天才重新分析
RUNBOOK_AMENDMENT_MIN_INCIDENTS = 3     # 累计 >= 3 次同类事故才触发
```

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
     -> 手动覆盖某个 discovery 结果
     -> Body: {"key": "metrics_service_label", "value": "my_label"}
```

---

## 9. DiscoveryRunner 集成点

worker 启动时在 `_build_deps()` 之前执行：

```python
# apps/worker/tasks.py

def _build_deps(db, settings, agent_run_id, incident_id):
    # 加载或执行 discovery
    discovery = _get_or_run_discovery(settings)

    # 合并配置
    effective = merge_settings(settings, discovery)

    # 构造 tools（用 effective config 替代 settings 默认值）
    metrics_tool = MetricsTool(
        base_url=settings.prometheus_url,
        service_label=effective.metrics_service_label,
        metric_patterns=effective.metric_patterns,  # 新增参数
        ...
    )
```

---

## 10. Alert Pull Mode

除了传统的 Alertmanager webhook push，agentp 支持主动轮询模式——后端无需配置 agentp 的 URL。

### 10.1 两种模式

| 模式 | 配置 | 适用场景 |
|------|------|---------|
| **Push (webhook)** | `ALERT_SOURCE=webhook` | Alertmanager 可配置 webhook，实时性最高 |
| **Pull (poll)** | `ALERT_SOURCE=poll` | Alertmanager 不可配 webhook，或作为兜底 |
| **Both** | `ALERT_SOURCE=both` | webhook 实时 + poll 兜底（Alertmanager 重启丢消息时补齐） |
| **None** | `ALERT_SOURCE=none` | 仅手动发告警（测试用） |

### 10.2 Pull 实现

Celery Beat 周期任务，调用 Alertmanager `GET /api/v2/alerts`：

```python
# apps/worker/tasks.py

@celery_app.task(bind=True)
def poll_alertmanager(self) -> dict:
    """Pull active alerts from Alertmanager. No webhook config needed on their side."""
    settings = get_settings()
    url = f"{settings.alertmanager_url.rstrip('/')}/api/v2/alerts"

    response = httpx.get(url, params={
        "silenced": "false",
        "inhibited": "false",
    }, timeout=10)
    response.raise_for_status()

    alerts = response.json()  # list of standard alertmanager alert dicts
    new_count = 0

    for alert in alerts:
        # Alertmanager /api/v2/alerts 返回的单个 alert 结构与 webhook
        # payload 中 alerts[] 条目一致，复用 _from_alertmanager_single_alert()
        normalized = _from_alertmanager_single_alert(alert)
        try:
            result = AlertService(...).create_alert(
                AlertCreateRequest.model_validate(normalized)
            )
            if not result.deduplicated:
                new_count += 1
        except Exception:
            # 单个告警失败不影响其他告警的拉取
            continue

    return {"polled": len(alerts), "new_incidents": new_count}
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
    fingerprint = _string(alert.get("fingerprint"), f"alertmanager:{service}:{alert_name}")

    return {
        "source": "alertmanager",
        "fingerprint": fingerprint,
        "service": service,
        "severity": _normalize_severity(labels.get("severity")),
        "alert_name": alert_name,
        "starts_at": _starts_at(alert.get("startsAt")),
        "ends_at": _ends_at(alert.get("endsAt")),
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
        "options": {"expires": 25},  # 上次还没跑完就跳过
    },
}
```

### 10.4 配置

```bash
# .env
ALERT_SOURCE=poll                          # webhook | poll | both | none
ALERTMANAGER_URL=http://alertmanager-main.monitoring.svc:9093
ALERT_POLL_INTERVAL_SECONDS=30
```

`ALERTMANAGER_URL` 可以被 K8s 基础设施发现自动填充（见 3.7），进一步减少手动配置。

### 10.5 与 Webhook 的去重

两种模式共用 `AlertService.create_alert()`，其内置的 fingerprint 去重逻辑确保：

- Webhook 先到 → 创建 incident → Pull 再拉到同一条 → deduplicated
- Pull 先拉到 → 创建 incident → Webhook 再到 → deduplicated
- Alertmanager 重启、webhook 丢消息 → Pull 补上

---

## 11. 安全考虑

| 风险 | 措施 |
|------|------|
| Discovery 探测 K8s API 权限过大 | 只用 read-only API：list pods/deployments/services/namespaces |
| 联网搜索泄露服务名 | 搜索查询只用故障类型通用描述 + 技术栈，不含服务真实名称 |
| 搜索结果含恶意链接 | 只抓文本，不执行 JS，不下载二进制 |
| LLM 幻觉指标名 | MetricMatcher 结果优先于 LLM 推断；不在 discovery 列表中的指标名自动标记 needs_human_verification |
| Runbook Amendment 引入错误知识 | 必须人工审批；基于 >= 3 次事故的统计规律，非单次偶然 |

---

## 12. 新用户接入体验

### K8s 环境（全自动）

```bash
# 唯一必须手动配置的
export LLM_PROVIDER=deepseek
export LLM_API_KEY=sk-xxx

# 其余全部自动发现 — 无需配置 PROMETHEUS_URL / LOKI_URL / JAEGER_URL / ALERTMANAGER_URL
# 告警来源也自动选择: 发现 Alertmanager → ALERT_SOURCE=poll（无需 Alertmanager 侧配置 webhook）

# 启动
docker compose up -d api worker web postgres redis

# worker 日志输出:
# [discovery] k8s backend: prometheus-k8s.monitoring.svc:9090
# [discovery] k8s backend: loki-gateway.monitoring.svc:3100
# [discovery] k8s backend: jaeger-query.observability.svc:16686
# [discovery] k8s backend: alertmanager-main.monitoring.svc:9093
# [discovery] 5/5 backends auto-discovered
# [discovery] detected service label: "app" (confidence: 0.95)
# [discovery] found 12 services: api-gateway, user-svc, ...
# [discovery] matched 8/8 metric types
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
export ALERT_SOURCE=webhook          # 非 K8s 默认 webhook 模式
export LLM_PROVIDER=deepseek
export LLM_API_KEY=sk-xxx

docker compose up -d api worker web postgres redis

# 后续在 Alertmanager 中配置 webhook 指向 agentp
```

---

## 13. 部署注意事项

### 12.1 精简服务栈

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
  BGE-ZH      :8083     → 如用 OpenAI embedding 可移除
  Mailpit     :8025     → 配置真实 SMTP 后移除
  demo-svc    :8080     → 故障注入 demo，不需要
  Promtail    (日志采集) → 已有 Loki + Promtail 栈
```

对应的 `docker-compose.yml` 只需要保留 4 个核心 service 定义，其余全部删掉或注释。

### 12.2 端口冲突处理

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

### 12.3 K8s 环境

如果 agentp 部署在 K8s 集群内（推荐），没有端口冲突问题——每个 Pod 有独立 IP。只需注意：

- K8s Discovery 需要 ServiceAccount 权限（read-only：list pods/deployments/services/namespaces）
- 可观测性后端通过 K8s Service DNS 访问：`http://prometheus-k8s.monitoring.svc:9090`
- PostgreSQL + Redis 可用 StatefulSet 或外部托管服务

### 12.4 Alertmanager Webhook 配置

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

### Phase 1: Core Discovery (3-4 天)
- `DiscoveryResult`、`MetricMapping`、`LabelConvention` 等数据模型
- `PromDiscovery` + `MetricMatcher`
- `ConfigMerge` 优先级系统
- 单元测试（mock Prometheus API 响应）

### Phase 2: K8s & Topology (2-3 天)
- `K8sDiscovery`（服务列表 + label 检测）
- `TopologyDeriver`（env var 推导）
- `LokiDiscovery`（label 交叉验证）
- 单元测试

### Phase 3: Runner & Degradation (2-3 天)
- `DiscoveryRunner` 编排 + 并行执行
- 逐组件降级逻辑
- Discovery 缓存（文件 + 过期策略）
- 集成测试（多环境 fixture 模拟）

### Phase 4: Runbook Generation (3-4 天)
- `RunbookTemplateEngine`（Jinja2 渲染）
- `RunbookWebSearcher`（联网搜索 + 内容提取）
- `LLMRunbookGenerator`（LLM 生成 + 自评）
- 审查 API（draft CRUD + review decision）

### Phase 5: Runbook Feedback (2-3 天)
- `RunbookFeedbackAnalyzer`（条件触发 + 差异分析）
- Amendment 数据模型 + 审批 API
- 反馈循环（审查结果 -> 生成 prompt 优化）

### Phase 6: API & Integration (2 天)
- Discovery API（status/services/metrics/topology/capabilities/rerun/override）
- 与 `_build_deps()` 集成
- 前端 Discovery 状态面板 + Runbook 审查界面

### Phase 7: Testing & Docs (2 天)
- 多环境集成测试
- 文档更新
- 端到端验证

**总计约 16-21 天**。Phase 1-3 可独立交付（解决 "快速适配" 核心痛点），Phase 4-6 增强 Runbook 智能化。
