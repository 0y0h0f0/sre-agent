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
        ├──> 第二层: 联网搜索 (可选, RUNBOOK_WEB_SEARCH_ENABLED)
        │       RunbookWebSearcher -> 外部 runbook / 故障案例 / 最佳实践
        │       并行搜索，去重，提取相关段落
        │
        └──> 第三层: LLM 生成 (可选, RUNBOOK_LLM_GENERATION_ENABLED)
                LLMRunbookGenerator -> 结合 discovery + 搜索结果
                输出: RunbookDraft (正文 + SelfCritique + 来源追溯)
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

### 6.3 LLM 生成 Prompt

```
System:
You are an SRE expert writing runbooks for a production service.

Rules:
- Use the ACTUAL metric names from the context, never invent placeholders
- Only include diagnostic steps executable with available tools
- Reference external sources when relevant, cite them
- Append a Self-Critique section

Context:
- Service: {service_name}
- Available Tools: {capabilities}
- Metric Mappings: {actual_metric -> semantic_type}
- Service Dependencies: {topology}
- External References: {search_results_summary}
- Template Skeleton: {template_outline}
```

### 6.4 RunbookDraft 数据结构

```python
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
    generated_at: datetime
    model: str
    status: Literal["pending_review", "reviewed", "rejected"] = "pending_review"
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
              ├── LLM 差异分析:
              │     ├── 对比本次诊断 vs 现有 Runbook
              │     ├── 识别缺失/过时内容
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

## 10. 安全考虑

| 风险 | 措施 |
|------|------|
| Discovery 探测 K8s API 权限过大 | 只用 read-only API：list pods/deployments/services/namespaces |
| 联网搜索泄露服务名 | 搜索查询只用故障类型通用描述 + 技术栈，不含服务真实名称 |
| 搜索结果含恶意链接 | 只抓文本，不执行 JS，不下载二进制 |
| LLM 幻觉指标名 | MetricMatcher 结果优先于 LLM 推断；不在 discovery 列表中的指标名自动标记 needs_human_verification |
| Runbook Amendment 引入错误知识 | 必须人工审批；基于 >= 3 次事故的统计规律，非单次偶然 |

---

## 11. 新用户接入体验

```bash
# 最小配置
export PROMETHEUS_URL=http://prom:9090
export LOKI_URL=http://loki:3100
export JAEGER_URL=http://jaeger:16686
export LLM_PROVIDER=deepseek
export LLM_API_KEY=sk-xxx

# 启动
docker compose up -d api worker web postgres redis

# worker 日志输出:
# [discovery] detected service label: "app" (confidence: 0.95)
# [discovery] found 12 services: api-gateway, user-svc, ...
# [discovery] matched 8/8 metric types
# [discovery] topology derived: 9 nodes, 14 edges (confidence: 0.72)
# [runbook] generated 5 drafts for user-svc (pending review)
# [runbook] generated 5 drafts for order-svc (pending review)

# 进入 Web UI:
# 1. 查看 Discovery 状态 -> 确认自动检测结果
# 2. 审查 Runbook 草稿 -> 批准/编辑/拒绝
# 3. 配置 Alertmanager webhook -> 完成
```

---

## 12. 实施路线

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
