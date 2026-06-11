# sre-agent Real Backend Integration Implementation Plan

**Date:** 2026-06-11
**Status:** draft
**Based on:** `2026-06-10-real-backend-integration-design(13).md`
**Target:** 将 sre-agent 从本地 demo/fixture 模式扩展为可接入真实 Prometheus + Loki + Jaeger + Kubernetes + Alertmanager 的生产安全诊断系统

---

## 0. Executive Summary

本施工文档将设计文档中 Phase 0–8 的任务拆分为 **44 个独立 PR**，按 9 个 Milestone 组织。每个 PR 可独立实现、独立 review、独立测试、独立回滚。

**核心原则：**
- 生产安全优先：`LLM_PROVIDER=disabled`、`EXECUTOR_BACKEND=fixture` 是生产默认值
- 逐级降级：Discovery 失败不阻塞 agent 启动
- 已发布配置唯一可信：worker 只使用 `published` 的 `EffectiveConfigVersion`
- 确定性优先于 LLM：Phase 0–8 不依赖 LLM 和 web_search
- 审计一切：所有配置变更写入不可变审计日志

**预计总工期：** Phase 0–8 约 19–25 个工作日（单开发者），可并行化后缩短。

---

## 1. Current Repository Assessment

### 1.1 模块清单

| Capability | Existing Module/File | Status | Reuse Strategy | Gap |
|-----------|----------------------|--------|----------------|-----|
| Settings | `packages/common/settings.py` | partial | **extend** — 添加 `APP_ENV`, `AUTOMATION_LEVEL`, `DISCOVERY_*`, `ALERT_SOURCE`, `ALERT_POLL_*`, `RUNBOOK_LLM_*`, `RUNBOOK_WEB_SEARCH_*`, `BACKEND_AUTH_*` | 缺少生产安全默认值、discovery 开关、poll 配置、auth config |
| Backend Protocol | `packages/tools/trace_backends.py`, `k8s.py`, `deployment_backends.py` | exists | **reuse** — 现有 `Protocol` + `build_*_backend()` 工厂模式可直接扩展 | 缺少 BackendAuthConfig 集成、degraded 语义 |
| MetricsTool | `packages/tools/metrics.py` | exists | **reuse** — 已有 PromQL 生成、query_range、缓存、shard | 硬编码 PromQL 模板，不支持 discovery 输出注入 |
| LogsTool | `packages/tools/logs.py` | exists | **reuse** — 已有 Loki query_range、缓存、聚合 | service_label 需来自 effective config |
| TraceTool | `packages/tools/traces.py` | exists | **reuse** — 已有 Backend Protocol（fixture/jaeger/tempo） | 需 auth config 集成 |
| K8sDiagnosticsTool | `packages/tools/k8s.py` | exists | **reuse** — 已有 read-only 约束、fixture/live backend | 需 auth config；LiveK8sBackend 需扩展 list APIs |
| DbDiagnosticsTool | `packages/tools/db_diagnostics.py` | exists | **reuse** | 无需修改 |
| GitChangeTool | `packages/tools/git_changes.py` + `deployment_backends.py` | exists | **reuse** | 需 auth config |
| AlertService | `apps/api/services/alert_service.py` | exists | **reuse** — 已有 fingerprint dedup、NFA suppression、diagnosis enqueue | 缺少 poll 模式入口、`alertmanager_poll` source |
| Alert Schemas | `apps/api/schemas/alerts.py` | exists | **extend** — 需添加 `_from_alertmanager_single_alert()` for poll mode、`AlertPollFilters` | 缺少 poll 解析 |
| Celery tasks | `apps/worker/tasks.py` | exists | **extend** — 需添加 `poll_alertmanager` task、DiscoveryRunner task、`_build_deps` 重构 | 缺少 discovery、poll、effective config integration |
| Celery app | `apps/worker/celery_app.py` | exists | **extend** — 添加 beat schedule | 缺少 beat 配置 |
| LangGraph workflow | `packages/agent/graph.py` | exists | **reuse** — 无需修改 | 诊断节点可继续使用 |
| Runner | `packages/agent/runner.py` | exists | **reuse** | 无需修改 |
| AgentDeps | `packages/agent/schemas.py` | exists | **extend** — 添加 `effective_config` 字段 | 缺少 effective config 传递 |
| Runbook memory | `packages/db/models.py` (`RunbookChunk`, `RunbookDraft`, `RunbookVersion`) | exists | **extend** — 添加 template draft、amendment draft 模型 | 缺少 template/amendment 区分、source_path 强制 |
| Runbook search | `packages/tools/runbook_search.py` + `packages/rag/` | exists | **reuse** | 无需修改 |
| PrometheusClient | not found in current scan | missing | **add** to `packages/discovery/prom_discovery.py` | 需要 label/__name__/values、labels、series、metadata、query API |
| LokiClient | not found in current scan | missing | **add** to `packages/discovery/loki_discovery.py` | 需要 /loki/api/v1/labels |
| JaegerClient | `packages/tools/trace_backends.py` (`JaegerTraceBackend`) | partial | **extend** — 添加 `/api/services` for service discovery | 缺少服务发现 API |
| K8sClient (discovery) | not found in current scan | missing | **add** to `packages/discovery/k8s_discovery.py` | 需要 list pods/deployments/services/namespaces |
| AlertmanagerClient | not found in current scan | missing | **add** to `packages/discovery/alertmanager_client.py` | 需要 GET /api/v2/alerts、/api/v2/status |
| Redis lock | not found in current scan | missing | **add** to `packages/common/redis_lock.py` | 需要 context manager 分布式锁 |
| DB migration framework | `migrations/` (Alembic) | exists | **reuse** — 添加新 migration | 正常 |
| Audit log | `packages/db/models.py` (`AuditLog`) + `packages/db/repositories/audit_logs.py` | exists | **extend** — 扩展 action 枚举、添加 metadata 字段 | 当前 action 过于简单，需支持 discovery/config 操作 |
| Test fixtures | `tests/conftest.py` | exists | **extend** — 添加 discovery mock、alertmanager mock、effective config fixtures | 缺少生产安全测试 fixtures |
| Evidence validation | `packages/agent/evidence_validation.py` | exists | **reuse** | 无需修改 |
| Topology | `packages/agent/topology.py` | exists | **extend** — 已有 `ServiceTopology`，需拆分 `WorkloadBinding` vs `ServiceEdge` | 当前不区分 binding 和 edge |
| FakeLLM | `packages/agent/fake_llm.py` | exists | **reuse** | 无需修改 |
| Guardrails | `packages/agent/guardrails/policy.py` | exists | **reuse** | 无需修改 |
| Executor backends | `packages/tools/executor_backends.py` | exists | **reuse** — 生产默认 fixture | 无需修改 |
| Mock executor | `packages/tools/mock_executor.py` | exists | **reuse** | 无需修改 |
| Runbook generator | `packages/rag/runbook_generator.py` | exists | **extend** — 添加确定性模板引擎 | 当前可能依赖 LLM，需改为纯模板 |
| Runbook Review API | `apps/api/routers/runbooks.py` | exists | **extend** — review 端点已部分存在 | 需添加 regenerate、draft review 增强 |
| Feedback model | `packages/db/models.py` (`FeedbackItem`) | exists | **extend** — 添加 `RunbookFeedbackSummary`、`AmendmentDraft` | 缺少确定性反馈摘要模型 |

### 1.2 关键依赖链

```
Settings (M0) → 所有模块的基础
  ├── Discovery Models (M0) → M1/M2/M3 的数据基础
  ├── AuditLog (M0) → 所有配置变更的审计基础
  ├── AutomationPolicy (M0) → M3 发布闭环的决策基础
  └── EffectiveConfig (M0) → M3/M5 worker 集成的读取基础

M1 (Prometheus Discovery) → M3 (DiscoveryRunner)
M2 (K8s/Loki/Topology) → M3 (DiscoveryRunner)
M3 (DiscoveryRunner + Config) → M5 (API + Worker Integration)
M4 (Alertmanager Poll) → M5 (Worker task integration)
M6 (Runbook Template) → M7 (Runbook Feedback)
M3 + M5 → M6 (需要 discovery 结果 + effective config)
M0-M7 → M8 (Testing & Docs)
```

### 1.3 影响拆分顺序的关键依赖

1. **M0 的 Settings + Models 是硬阻塞**：所有后续 milestone 都依赖生产默认值和数据模型
2. **M1 和 M2 可以并行**：Prometheus discovery 和 K8s discovery 互不依赖
3. **M3 依赖 M1 + M2 的结果模型**：DiscoveryRunner 编排需要所有 discovery 的输出类型
4. **M4 可以与 M1/M2/M3 并行**：Alertmanager poll 只依赖 Settings (M0)，不依赖 discovery
5. **M5 依赖 M3**：API 发布的是 M3 产出的 EffectiveConfigVersion
6. **M6 依赖 M3**：Runbook 模板需要 discovery 结果（服务名、能力矩阵、指标映射）
7. **M7 依赖 M6**：Runbook 反馈需要已有的 runbook 结构

---

## 2. Implementation Principles

1. **Production-safe by default** — `LLM_PROVIDER=disabled`, `EXECUTOR_BACKEND=fixture`, `APP_ENV=production` 所有安全默认值
2. **Read-only first** — 所有 discovery 和诊断工具只读；写入必须经过 guardrail → approval → second confirmation
3. **Degraded instead of failed** — 局部后端不可达时标记 degraded，不阻塞整体 agent
4. **Published config only** — worker 只使用 `published` 的 `EffectiveConfigVersion`，不读取未审核 proposal
5. **Audit everything** — 所有配置变更（publish/rollback/revoke/override/auto_apply/reject）写入不可变审计日志
6. **Fixture/demo compatibility** — 所有修改保持 `APP_ENV=local` + fixture 默认值不变，CI 使用 FakeLLM
7. **LLM disabled by default** — Phase 0–8 不启用 LLM；Phase 9+ 需要 `RUNBOOK_LLM_GENERATION_ENABLED=true` 显式开启
8. **No hidden localhost fallback in production** — 生产环境未配置后端 URL 时返回 `unavailable/degraded`，不回退到 `localhost`
9. **Deterministic before LLM** — 所有诊断和 Runbook 能力先用确定性方法实现
10. **Small PRs, independently testable** — 每个 PR 可独立 review、测试、部署、回滚
11. **Immutable audit** — 审计日志创建后不可修改或删除
12. **Token/secret never in LLM prompt** — 认证凭据在 LLM 调用前脱敏
13. **No unpublished proposal in worker** — 生产 worker 绝不读取未发布的 discovery proposal
14. **Discovery failure != agent failure** — Discovery 失败不阻塞 agent 启动和诊断

---

## 3. Milestone Overview

| Milestone | Goal | Depends On | Deliverable | Can Start In Parallel |
|----------|------|------------|-------------|-----------------------|
| **M0** | Production safety foundation | none | Settings defaults, data models, audit, automation policy, effective config | no (blocks all) |
| **M1** | Prometheus core discovery | M0 (settings + models) | PrometheusClient, MetricCandidate, MetricMatcher, PromQL Builder, PromQL Validator | with M2, M4 |
| **M2** | K8s / Loki / Topology discovery | M0 | K8sDiscovery, LabelDetector, LokiDiscovery, WorkloadBinding, ServiceEdge Deriver | with M1, M4 |
| **M3** | DiscoveryRunner + EffectiveConfig 发布闭环 | M1 + M2 | DiscoveryRunner, degradation output, cost control, DiscoveryStore, Config proposal/publish/rollback | no (depends on M1+M2) |
| **M4** | Alertmanager poll production hardening | M0 | AlertmanagerClient, MatcherParser, Scope validation, Poll Cursor, Resolved inference, Poll task | with M1, M2, M3 |
| **M5** | Discovery API / Operator API | M3 + M4 | Read APIs, Write APIs, Override API, worker `_build_deps` integration | no (depends on M3) |
| **M6** | Runbook template generation | M3 | RunbookTemplateEngine, RunbookDraft (template type), Review API, Approved ingest | no (depends on M3) |
| **M7** | Deterministic runbook feedback | M6 | Incident aggregation, action statistics, gap detection, AmendmentDraft | no (depends on M6) |
| **M8** | Testing & docs | M0-M7 | Unit, integration, production safety, E2E tests; docs | no (depends on M0-M7) |
| **M9+** | Future extensions | M8 | LLM runbook, web search, Tempo, Grafana | informational only |

---

## 4. Detailed Milestone Plan

### M0: Production Safety Foundation

**目标：** 打牢生产安全底座，确保后续真实后端接入后不会出现越权、误用 proposal、误用 localhost、LLM 默认启用等问题。

**为什么排第一：** 所有后续 Milestone 都依赖生产默认值、数据模型、审计基础设施和自动化策略。

**前置依赖：** 无

**不做：**
- 不实现任何 discovery 逻辑
- 不实现任何 API 端点（除了可能需要的内部读取路径）
- 不修改 worker `_build_deps()`（这是 M5 的工作）
- 不实现 alert poll

**涉及的主要模块：**
- `packages/common/settings.py`
- `packages/db/models.py`
- `packages/db/repositories/audit_logs.py`
- `migrations/`

---

#### PR 0.1: Settings 生产默认值

##### 背景

当前 settings.py 缺少 `APP_ENV` 区分，所有后端 URL 默认 `localhost`，`LLM_PROVIDER` 默认 `fake`。需要引入生产安全默认值。

##### 范围

- [x] 新增 `APP_ENV` setting（`local` | `production`）
- [x] 生产环境 `LLM_PROVIDER` 默认 `"disabled"`（本地保持 `"fake"`）
- [x] 新增 `AUTOMATION_LEVEL`（`off` | `propose` | `supervised` | `autopilot`，默认 `supervised`）
- [x] 新增 `DISCOVERY_ENABLED`（默认 `true`）
- [x] 新增 `DISCOVERY_APPLY_MODE`（`inherit` | `propose` | `supervised`，默认 `inherit`）
- [x] 新增 `RUNBOOK_TEMPLATE_GENERATION_ENABLED`（默认 `true`）
- [x] 新增 `RUNBOOK_LLM_GENERATION_ENABLED`（默认 `false`）
- [x] 新增 `RUNBOOK_WEB_SEARCH_ENABLED`（默认 `false`）
- [x] 新增 `ALERT_SOURCE`（`webhook` | `poll` | `both` | `none`，默认 `webhook`）
- [x] 新增所有 `ALERT_POLL_*` 配置项（见设计文档 §10.4）
- [x] 新增 `BackendAuthConfig` 相关配置
- [x] 确保现有 settings 字段保持兼容，本地 demo 不受影响

##### 不做

- 不实现配置优先级合并逻辑（PR 0.4）
- 不实现生产启动时的 URL 校验

##### 建议文件

```text
packages/common/settings.py                         # 修改：新增 ~40 个字段
tests/unit/test_settings_production_defaults.py     # 新增
```

##### 测试清单

```text
test_production_llm_default_disabled
test_production_executor_default_fixture
test_local_can_use_localhost_defaults
test_local_llm_default_fake
test_automation_level_default_supervised
test_discovery_apply_mode_inherit
test_runbook_llm_default_false
test_runbook_web_search_default_false
test_alert_source_default_webhook
test_backward_compat_existing_fields
```

##### 验收标准

- [ ] `APP_ENV=production` + 未设置 `LLM_PROVIDER` → `llm_provider == "disabled"`
- [ ] `APP_ENV=local` + 未设置 `PROMETHEUS_URL` → `prometheus_url == "http://localhost:9090"`
- [ ] 所有新增字段可通过 env var 设置
- [ ] 现有测试全部通过（不修改测试代码）
- [ ] `ruff check` + `mypy` 无错误

##### 风险点

- 新增字段与现有代码中硬编码的 `settings.xxx` 不冲突（均为新增字段）
- `LLM_PROVIDER` 的默认值逻辑需要根据 `APP_ENV` 区分

##### 回滚方案

- 将 settings.py 恢复到修改前版本
- 所有新增字段在未设置时均有安全默认值，不影响现有功能

---

#### PR 0.2: Discovery / EffectiveConfig / AuditLog 数据模型与迁移

##### 背景

设计文档定义了 `DiscoveryRun`, `DiscoveryProposal`, `EffectiveConfigVersion`, `DiscoveryOverride`, `AutomationDecision` 等核心模型。需要创建 DB 模型和 Alembic 迁移。

##### 范围

- [x] 新增 DB 模型：`DiscoveryRun`, `DiscoveryProposal`, `EffectiveConfigVersion`, `DiscoveryOverride`
- [x] 扩展 `AuditLog` 模型：添加 `metadata` JSONB 字段（如不存在）、扩展 action/resource_type
- [x] 生成 Alembic 迁移
- [x] 新增 Pydantic schema（用于 API 层，与 DB 模型分离）

##### 不做

- 不实现 AutomationPolicy 的判定逻辑（PR 0.3）
- 不实现 DiscoveryRunner（M3）
- 不实现 API 端点（M5）

##### 建议文件

```text
packages/db/models.py                               # 修改：新增 4 个模型类，扩展 AuditLog
packages/db/repositories/discovery_runs.py          # 新增
packages/db/repositories/discovery_proposals.py     # 新增
packages/db/repositories/effective_configs.py       # 新增
packages/db/repositories/discovery_overrides.py     # 新增
migrations/versions/XXXX_discovery_config_models.py # 新增
tests/unit/test_discovery_models.py                 # 新增
```

##### 测试清单

```text
test_discovery_run_create
test_discovery_proposal_status_flow
test_effective_config_version_lifecycle
test_discovery_override_expiry
test_audit_log_supports_discovery_actions
test_migration_upgrade_downgrade
```

##### 验收标准

- [ ] 所有新表在 PostgreSQL 中创建成功
- [ ] FK 约束正确（proposal → discovery_run, config_version → proposal）
- [ ] AuditLog 可记录 `discovery.auto_apply`, `config.publish`, `config.rollback` 等 action
- [ ] Alembic upgrade/downgrade 均可正常执行
- [ ] 现有测试不受影响

##### 风险点

- AuditLog 扩展可能导致现有 repository 测试需要更新
- DiscoveryProposal 的 `config_diff` JSONB 可能很大

##### 回滚方案

- Alembic downgrade 删除新表
- 恢复 models.py 中 AuditLog 的原有定义

---

#### PR 0.3: AutomationPolicy

##### 背景

设计文档 §3.8 定义了 `AutomationDecision` 模型和自动化判定规则。

##### 范围

- [x] 实现 `AutomationPolicy` 类（纯函数，无副作用）
- [x] 实现 `AutomationDecision` 的判定逻辑
- [x] 实现 `DISCOVERY_APPLY_MODE` 不能超过 `AUTOMATION_LEVEL` 的校验
- [x] 实现各类配置的自动发布条件（后端 URL、service label、metric mapping）
- [x] 确保 `EXECUTOR_BACKEND=live` 永不可自动发布

##### 不做

- 不实现数据库读写（纯逻辑）
- 不实现 API 调用

##### 建议文件

```text
packages/discovery/automation_policy.py            # 新增
tests/unit/test_automation_policy.py               # 新增
```

##### 测试清单

```text
test_off_returns_record_only
test_propose_returns_record_only
test_supervised_high_confidence_auto_apply
test_supervised_low_confidence_require_review
test_autopilot_threshold_lower_than_supervised
test_executor_live_never_auto_apply
test_apply_mode_more_aggressive_rejected
test_apply_mode_more_conservative_allowed
test_apply_mode_inherit_equals_automation_level
test_backend_url_k8s_discovery_auto_apply
test_service_label_two_sources_cross_validated_auto_apply
test_metric_mapping_all_checks_pass_auto_apply
test_metric_mapping_metadata_missing_not_auto_apply
```

##### 验收标准

- [ ] 所有判定规则被测试覆盖
- [ ] 永不自动发布 `EXECUTOR_BACKEND=live`
- [ ] `DISCOVERY_APPLY_MODE > AUTOMATION_LEVEL` 时抛出异常
- [ ] 纯函数，无副作用，可独立单元测试

##### 风险点

- 自动发布条件过严可能阻碍正常使用；过松可能导致低质量配置进入生产

##### 回滚方案

- 删除 `automation_policy.py`，不影响其他模块

---

#### PR 0.4: EffectiveConfig 读取优先级

##### 背景

设计文档 §5 定义了生产配置优先级：`env > active override > published EffectiveConfigVersion > profile > safe default`。

##### 范围

- [x] 实现 `EffectiveConfig` Pydantic 模型
- [x] 实现 `EffectiveConfig.from_operator_sources()`（生产路径）
- [x] 实现 `EffectiveConfig.from_demo_sources()`（本地 demo 路径）
- [x] 实现 `_resolve_backend()` 函数（env > discovery > default，生产禁止 localhost fallback）
- [x] 实现 `load_published_effective_config()` 数据库读取
- [x] 实现 `has_unresolved_required_sources()` 检测

##### 不做

- 不修改 worker `_build_deps()`（M5 PR 5.5）

##### 建议文件

```text
packages/discovery/config_merge.py                  # 新增
tests/unit/test_config_merge.py                     # 新增
```

##### 测试清单

```text
test_env_has_highest_priority
test_override_beats_published_config
test_published_config_used_by_worker
test_unpublished_proposal_not_used
test_expired_config_not_used
test_production_rejects_implicit_localhost_backend
test_local_can_use_localhost_defaults
test_demo_path_uses_latest_discovery
test_missing_prometheus_url_unresolved
test_has_unresolved_returns_true_when_required_missing
```

##### 验收标准

- [ ] 生产路径 `allow_discovery_proposals=False` 时 proposal 不进入 config
- [ ] 生产路径未配置的 URL 返回 `None`，不返回 `localhost`
- [ ] Demo 路径保持向后兼容
- [ ] 优先级严格按 `env > override > published > profile > default`

##### 风险点

- DB 不可达时需要降级处理（返回 None）
- `EffectiveConfig` 字段需要与 `Settings` 保持一致但语义不同

##### 回滚方案

- 删除 `config_merge.py`

---

#### PR 0.5: AuditLog 服务扩展

##### 背景

现有 `AuditLog` 和 `AuditLogRepository` 需要扩展以支持 discovery/config 操作审计。

##### 范围

- [x] 扩展 `AuditLogRepository`：添加 `create_config_audit()`, `create_discovery_audit()`, `query_by_action()`, `query_by_target()`
- [x] 确保 audit log 不可变（无 update/delete 方法）
- [x] 添加 `source` 和 `request_id` 字段到 AuditLog 模型（如尚未存在）

##### 不做

- 不实现 API 端点
- 不实现审计日志清理/归档

##### 建议文件

```text
packages/db/repositories/audit_logs.py              # 修改：扩展方法
packages/db/models.py                               # 可能修改：AuditLog 字段扩展
migrations/versions/XXXX_audit_log_extensions.py    # 新增（如需要）
tests/unit/test_audit_log_extended.py               # 新增
```

##### 测试清单

```text
test_audit_log_config_publish
test_audit_log_config_rollback
test_audit_log_config_revoke
test_audit_log_discovery_auto_apply
test_audit_log_discovery_reject
test_audit_log_override_create
test_audit_log_immutable_no_update
test_audit_log_immutable_no_delete
test_query_by_action
test_query_by_target
test_query_by_time_range
```

##### 验收标准

- [ ] 所有配置变更操作有对应审计日志方法
- [ ] 审计日志创建后不可修改或删除
- [ ] 查询支持按 action、target、actor、时间范围筛选

##### 风险点

- AuditLog 模型字段扩展需要新的 migration

##### 回滚方案

- 回退 audit_logs.py 到修改前版本

---

### M1: Prometheus Core Discovery

**目标：** 完成核心 metric discovery 和 metric mapping——自动发现 Prometheus 指标名、匹配语义类型、生成参数化 PromQL 并 dry-run 验证。

**为什么排第二：** Prometheus 是最核心的信号源。MetricMatcher 是 discovery 的核心引擎。

**前置依赖：** M0（settings + discovery 数据模型 + automation policy）

**不做：**
- 不做 K8s/Loki discovery（M2）
- 不做 DiscoveryRunner 编排（M3）

---

#### PR 1.1: Discovery 基础 Pydantic 模型

##### 背景

设计文档 §3.3 定义了核心 Pydantic 模型和 §3.4 的 `SEMANTIC_PATTERNS` 模板库。

##### 范围

- [x] 实现所有 §3.3 定义的 Pydantic 模型
- [x] 实现 `MetricCandidate` dataclass（§3.4）
- [x] 实现 `SEMANTIC_PATTERNS` 模板库（覆盖 latency, error_rate, qps, cpu_throttle, disk_avail）
- [x] 实现 `DiscoveryCostControl` 模型（§3.10）

##### 不做

- 不实现任何客户端逻辑
- 不实现任何 DB 操作

##### 建议文件

```text
packages/discovery/__init__.py                      # 新增
packages/discovery/models.py                        # 新增
tests/unit/test_discovery_models_validation.py      # 新增
```

##### 测试清单

```text
test_metric_mapping_available
test_metric_mapping_degraded
test_metric_mapping_unavailable
test_metric_candidate_regex_compiles
test_semantic_patterns_all_have_promql_builder
test_semantic_patterns_latency_requires_le
test_semantic_patterns_error_rate_has_status_label
test_discovery_result_serialization
```

##### 验收标准

- [ ] 所有 Pydantic 模型可正常实例化和序列化
- [ ] `SEMANTIC_PATTERNS` 覆盖 5 种语义类型
- [ ] `MetricCandidate` 的 regex 全部可编译

##### 风险点

- 模板库可能过于宽松或严格，需要在真实数据上迭代

##### 回滚方案

- 删除 `models.py`

---

#### PR 1.2: PrometheusClient

##### 背景

需要 HTTP 客户端封装 Prometheus API 的 6 个端点。

##### 范围

- [x] 实现 `PrometheusClient` 类
- [x] 支持 `GET /api/v1/label/__name__/values`, `/labels`, `/series`, `/metadata`, `/query`, `/query_range`
- [x] 集成 `BackendAuthConfig`（bearer token、basic auth、mTLS、TLS verify）
- [x] 超时、错误映射、响应大小限制

##### 不做

- 不实现 MetricMatcher、PromQL Builder

##### 建议文件

```text
packages/discovery/prom_discovery.py                # 新增（含 PrometheusClient）
tests/unit/test_prometheus_client.py                # 新增
```

##### 测试清单

```text
test_list_metrics_success
test_list_metrics_timeout_degraded
test_list_metrics_auth_error
test_list_series_success
test_get_metadata_missing_metric
test_get_metadata_type_mismatch
test_range_query_empty_result
test_response_size_limit_truncated
test_bearer_token_auth_header
test_tls_verify_false
```

##### 验收标准

- [ ] 所有 6 个 API 端点可正常调用
- [ ] 超时和认证错误映射为明确异常
- [ ] 响应大小超过限制时截断并记录 warning

##### 风险点

- `/api/v1/series` 可能返回大量数据，需要 `match[]` 限定
- 认证配置需要与 httpx 正确集成

##### 回滚方案

- 删除 PrometheusClient 部分

---

#### PR 1.3: MetricMatcher 匹配引擎

##### 背景

MetricMatcher 是 discovery 的核心：对 Prometheus 指标列表进行语义匹配，按优先级尝试候选正则，通过 `/series` 验证 label 存在性，通过 `/metadata` 验证 metric type 和 unit。

##### 范围

- [x] 实现 `MetricMatcher.match()` 核心算法
- [x] 实现 label 验证（`required_any_labels` 至少存在一个）
- [x] 实现 metadata 验证（type、unit 校验）
- [x] 实现第一候选失败 → fallback 第二候选
- [x] 实现状态规则（available/degraded/unavailable）
- [x] 实现已存在 series 但当前窗口无数据 → degraded

##### 不做

- 不实现 PromQL dry-run（PR 1.6）

##### 建议文件

```text
packages/discovery/metric_matcher.py                # 新增
tests/unit/test_metric_matcher.py                   # 新增
```

##### 测试清单

```text
test_match_latency_histogram
test_match_error_rate_requests_total
test_missing_status_label_degraded
test_metadata_type_mismatch_rejected
test_no_candidate_unavailable
test_first_candidate_failed_fallback_second
test_error_rate_uses_5xx_filter
test_error_rate_uses_clamp_min
test_gauge_does_not_use_rate
test_too_many_series_rejected
test_timeout_degraded
```

##### 验收标准

- [ ] 5 种语义类型均可正确匹配
- [ ] label 缺失时标记 degraded
- [ ] metadata type/unit 不匹配时标记 degraded
- [ ] 所有候选失败时标记 unavailable

##### 风险点

- 正则匹配可能误匹配
- Metadata API 某些 Prometheus 版本不支持，需降级

##### 回滚方案

- 删除 `metric_matcher.py`

---

#### PR 1.4: Prometheus Service Label 检测

##### 范围

- [x] 实现 `detect_metrics_service_label()` 方法
- [x] 候选 key 列表 + 覆盖率 >= 80% 当选
- [x] 低覆盖率时标记低置信

##### 建议文件

```text
packages/discovery/prom_discovery.py                # 修改
tests/unit/test_prometheus_label_detection.py       # 新增
```

---

#### PR 1.5: PromQL Builder

##### 范围

- [x] 实现 5 种 PromQL 模板生成器
- [x] 参数化：service_label, service_name, metric_name 可注入

##### 建议文件

```text
packages/discovery/promql_builder.py                # 新增
tests/unit/test_promql_builder.py                   # 新增
```

##### 测试清单

```text
test_histogram_quantile_generates_correct_promql
test_error_rate_includes_clamp_min
test_error_rate_uses_5xx_filter
test_rate_qps_uses_sum_rate
test_gauge_does_not_use_rate
```

---

#### PR 1.6: PromQL Dry-Run 验证

##### 范围

- [x] 实现 `PromQLValidator` 类
- [x] 多窗口 dry-run（`[5m]`, `[1h]`, `[6h]`）
- [x] 空结果降级逻辑、series 数上限校验

##### 建议文件

```text
packages/discovery/promql_validator.py              # 新增
tests/unit/test_promql_validator.py                 # 新增
```

##### 测试清单

```text
test_current_window_has_data_ok
test_current_empty_but_1h_has_data_degraded
test_all_windows_empty_degraded_not_unavailable_if_series_exists
test_all_windows_empty_no_series_unavailable
test_too_many_series_rejected
```

---

### M2: K8s / Loki / Topology Discovery

**目标：** 补齐服务发现、label convention 检测、日志 label 检测和拓扑推导。

**前置依赖：** M0

**不做：** DiscoveryRunner 编排（M3）、API 端点（M5）

---

#### PR 2.1: K8sDiscovery

##### 范围

- [x] 实现 `K8sDiscovery` 类
- [x] 支持 list namespaces, pods, deployments, statefulsets, daemonsets, services
- [x] 集成 `namespace_allowlist`, `service_allowlist`
- [x] RBAC 不足时降级

##### 建议文件

```text
packages/discovery/k8s_discovery.py                 # 新增
tests/unit/test_k8s_discovery.py                    # 新增
```

##### 测试清单

```text
test_discover_deployments
test_discover_statefulsets
test_discover_daemonsets
test_namespace_allowlist
test_rbac_forbidden_degraded
test_k8s_unavailable_returns_empty_services
test_list_services_includes_selector
test_pod_sample_ratio
```

---

#### PR 2.2: Service Label Detector

##### 范围

- [x] 实现 `detect_k8s_service_label()`
- [x] 交叉验证：K8s label 与 metrics label 一致性
- [x] 输出 `LabelConvention`

##### 建议文件

```text
packages/discovery/label_detector.py                # 新增
tests/unit/test_label_detector.py                   # 新增
```

##### 测试清单

```text
test_k8s_label_coverage_selects_highest
test_metrics_label_can_differ_from_k8s
test_low_coverage_requires_review
test_alternatives_recorded_when_multiple_candidates
test_cross_validation_increases_confidence
```

---

#### PR 2.3: LokiDiscovery

##### 范围

- [x] 实现 `LokiClient` 类
- [x] 实现 `detect_logs_service_label()`
- [x] 集成 `BackendAuthConfig`

##### 建议文件

```text
packages/discovery/loki_discovery.py                # 新增
tests/unit/test_loki_discovery.py                   # 新增
```

##### 测试清单

```text
test_loki_list_labels_success
test_loki_unavailable_degraded
test_loki_label_can_differ_from_metrics
test_detect_logs_service_label
```

---

#### PR 2.4: WorkloadBinding

##### 范围

- [x] 实现 Service selector → Pod labels → ownerRef → Workload 推导链
- [x] 确保不产生 `ServiceEdge`

##### 建议文件

```text
packages/discovery/topology.py                      # 新增
tests/unit/test_workload_binding.py                 # 新增
```

##### 测试清单

```text
test_service_selector_to_deployment_binding
test_service_selector_never_creates_service_edge
test_missing_owner_ref_no_binding
```

---

#### PR 2.5: ServiceEdge Deriver

##### 范围

- [x] 四种策略按信度排序：manual (1.0) > trace (0.8-0.95) > env (0.5-0.7) > configmap (0.4-0.7)
- [x] 输出 `ServiceEdge` 列表

##### 建议文件

```text
packages/discovery/topology.py                      # 修改
tests/unit/test_service_edge_deriver.py             # 新增
```

##### 测试清单

```text
test_manual_topology_highest_priority
test_trace_call_graph_edge
test_env_var_dns_edge
test_configmap_edge
test_conflicting_edges_higher_confidence_wins
test_no_evidence_returns_empty
test_edge_has_evidence_field
```

---

### M3: DiscoveryRunner + EffectiveConfig 发布闭环

**目标：** 把 discovery 从独立函数变成完整运行流程，建立 proposal → decision → publish → worker 的闭环。

**前置依赖：** M1 + M2 + M0

---

#### PR 3.1: DiscoveryRunner 编排

##### 范围

- [x] 编排 K8sDiscovery + PromDiscovery + LokiDiscovery + TopologyDeriver
- [x] 并行执行，局部失败不影响整体
- [x] 输出 `DiscoveryResult` + warnings + 降级字段

##### 建议文件

```text
packages/discovery/runner.py                        # 新增
tests/unit/test_discovery_runner.py                 # 新增
```

##### 测试清单

```text
test_runner_success_all_backends
test_runner_prometheus_down_degraded
test_runner_k8s_rbac_forbidden_degraded
test_runner_loki_down_degraded
test_missing_latency_metric_adds_capability_gap
test_runner_output_includes_degraded_signals
```

---

#### PR 3.2: 降级输出标准化

##### 范围

- [x] 实现 `CapabilityAssessor`
- [x] 标准化降级字段（`capability_gaps`, `degraded_signals`, `used_fallback_signals`, `confidence_adjustment`）

##### 建议文件

```text
packages/discovery/capability_assessor.py           # 新增
tests/unit/test_capability_assessor.py              # 新增
```

---

#### PR 3.3: 成本控制

##### 范围

- [x] 实现 `DiscoveryCostController`
- [x] Prometheus/K8s 查询限制 + 结果截断 + warning

##### 建议文件

```text
packages/discovery/cost_control.py                  # 新增
tests/unit/test_discovery_cost_control.py           # 新增
```

##### 测试清单

```text
test_metric_names_truncated_with_warning
test_series_over_limit_rejected
test_pod_sample_ratio_applied
test_cache_hit_avoids_api_call
```

---

#### PR 3.4: DiscoveryStore

##### 范围

- [x] 实现 `DiscoveryStore` — 持久化 DiscoveryRun + DiscoveryProposal
- [x] 作为 Celery task 运行（`run_discovery`）

##### 建议文件

```text
packages/discovery/store.py                         # 新增
apps/worker/tasks.py                                # 修改：添加 run_discovery task
apps/worker/celery_app.py                           # 修改：beat schedule
tests/unit/test_discovery_store.py                  # 新增
tests/integration/test_discovery_task.py            # 新增
```

##### 测试清单

```text
test_runner_persists_discovery_run
test_discovery_run_status_succeeded
test_discovery_run_status_degraded
test_discovery_proposal_created
test_proposal_diff_matches_changes
```

---

#### PR 3.5: Config Proposal 生成

##### 范围

- [x] 实现 `ConfigProposalGenerator` — 比较 discovery 与当前 config，生成 diff
- [x] 调用 `AutomationPolicy.evaluate_all()`

##### 建议文件

```text
packages/discovery/config_proposal.py               # 新增
tests/unit/test_config_proposal.py                  # 新增
```

##### 测试清单

```text
test_proposal_not_published_by_default
test_proposal_diff_empty_when_no_changes
test_proposal_high_confidence_no_review
test_proposal_low_confidence_requires_review
test_proposal_rejected_when_dangerous
```

---

#### PR 3.6: EffectiveConfigVersion Publish / Rollback / Revoke

##### 范围

- [x] 实现 `publish_config()`, `rollback_config()`, `revoke_config()`
- [x] 版本过期策略（默认 30 天）
- [x] 所有操作写入 audit log

##### 建议文件

```text
packages/discovery/config_publisher.py              # 新增
tests/unit/test_config_publisher.py                 # 新增
```

##### 测试清单

```text
test_publish_config_creates_audit_log
test_publish_expires_previous_version
test_rollback_restores_previous_version
test_rollback_creates_audit_log
test_revoke_removes_from_worker_selection
test_revoke_creates_audit_log
test_expired_config_not_used_by_worker
test_unpublished_proposal_not_publishable_without_review
test_version_staleness_warning_before_expiry
```

---

### M4: Alertmanager Poll Production Hardening

**目标：** 支持后端项目零改动接入 Alertmanager poll，保证不拉全量、不误判 resolved。

**前置依赖：** M0

**可以与其他 Milestone 并行**

---

#### PR 4.1: AlertmanagerClient

##### 范围

- [x] 实现 `AlertmanagerClient`（`GET /api/v2/alerts`, `/api/v2/status`）
- [x] 集成 `BackendAuthConfig`

##### 建议文件

```text
packages/discovery/alertmanager_client.py           # 新增
tests/unit/test_alertmanager_client.py              # 新增
```

---

#### PR 4.2: Matcher Parser

##### 范围

- [x] 实现 `parse_matchers()` + `to_alertmanager_filter()`
- [x] 支持 `=`, `!=`, `=~`, `!~` 四种操作符

##### 建议文件

```text
packages/discovery/matcher_parser.py                # 新增
tests/unit/test_matcher_parser.py                   # 新增
```

##### 测试清单

```text
test_parse_equal
test_parse_not_equal
test_parse_regex
test_parse_not_regex
test_invalid_matcher_rejected
test_quoted_comma_not_split
test_label_name_validation
test_invalid_regex_rejected
test_to_alertmanager_filter_format
test_escape_matcher_value
```

---

#### PR 4.3: Scope Validation

##### 范围

- [x] 实现 `AlertPollFilters` + `has_valid_scope()`
- [x] severity-only 不算有效 scope

##### 建议文件

```text
packages/discovery/matcher_parser.py                # 修改
tests/unit/test_poll_scope_validation.py            # 新增
```

##### 测试清单

```text
test_severity_only_not_valid_scope
test_non_severity_matcher_valid_scope
test_receiver_valid
test_namespace_allowlist_valid
test_service_allowlist_valid
test_empty_scope_disabled
test_mixed_severity_and_namespace_valid
```

---

#### PR 4.4: Allowlist Server-side Filter

##### 范围

- [x] 实现 `_allowlist_to_server_matchers()`
- [x] 生产无法映射 → disabled；非生产 → client-side filter + warning

##### 建议文件

```text
packages/discovery/matcher_parser.py                # 修改
tests/unit/test_allowlist_to_matcher.py             # 新增
```

##### 测试清单

```text
test_namespace_allowlist_to_regex_matcher
test_service_allowlist_to_regex_matcher
test_unmappable_allowlist_production_disabled
test_unmappable_allowlist_local_client_filter
test_matchers_added_before_list_alerts
```

---

#### PR 4.5: Poll Cursor / Dedup

##### 范围

- [x] 实现 `AlertPollCursor` DB 模型 + repository
- [x] `already_seen_active()` 必须更新 seen 状态（即使返回 True）
- [x] `mark_seen()` 建立 fingerprint → incident_id 映射

##### 建议文件

```text
packages/db/models.py                               # 修改：添加 AlertPollCursor
packages/db/repositories/poll_cursor.py             # 新增
migrations/versions/XXXX_poll_cursor.py             # 新增
tests/unit/test_poll_cursor.py                      # 新增
```

##### 测试清单

```text
test_existing_fingerprint_seen
test_already_seen_updates_last_seen
test_mark_seen_updates_current_set
test_cursor_persist_and_load
test_already_seen_returns_true_but_still_updates_state
```

---

#### PR 4.6: Resolved Inference

##### 范围

- [x] 实现 `infer_resolved_from_missing_fingerprints()`
- [x] truncation 时禁止 resolved 推断

##### 建议文件

```text
packages/discovery/resolved_inference.py            # 新增
tests/unit/test_resolved_inference.py               # 新增
```

##### 测试清单

```text
test_missing_enough_rounds_resolved
test_grace_period_blocks_resolved
test_truncation_blocks_resolved_inference
test_first_missing_not_resolved
test_reappeared_resets_missing_counter
```

---

#### PR 4.7: Poll Task + Redis Lock + Metrics + Audit

##### 范围

- [x] 实现 `poll_alertmanager` Celery task（完整集成）
- [x] 实现 Redis 分布式锁（`packages/common/redis_lock.py`）
- [x] 实现 poll 指标
- [x] 实现 `_from_alertmanager_single_alert()` 解析（添加到 `apps/api/schemas/alerts.py`）
- [x] Beat schedule 配置

##### 建议文件

```text
apps/worker/tasks.py                                # 修改：添加 poll_alertmanager task
packages/common/redis_lock.py                       # 新增
apps/worker/celery_app.py                           # 修改：beat schedule
apps/api/schemas/alerts.py                          # 修改：_from_alertmanager_single_alert
tests/unit/test_poll_task.py                        # 新增
tests/integration/test_poll_integration.py          # 新增
```

##### 测试清单

```text
test_poll_creates_incident
test_poll_deduplicates_existing
test_poll_severity_only_scope_disabled
test_poll_receiver_valid_scope
test_lock_prevents_concurrent_poll
test_failure_records_metric_and_audit
test_truncation_skips_resolved_inference
test_poll_resolved_inference
test_from_alertmanager_single_alert_parse
test_lock_key_includes_effective_filter
test_audit_safe_dict_contains_allowlist_fields
```

---

### M5: Discovery API / Operator API

**目标：** 提供查看 discovery、触发 rerun、发布/回滚配置、设置 override 的 API。

**前置依赖：** M3 + M4

---

#### PR 5.1: Discovery Read API

##### 范围

- [x] `GET /api/discovery/status`, `/services`, `/metrics`, `/topology`, `/capabilities`

##### 建议文件

```text
apps/api/routers/discovery.py                       # 新增
apps/api/schemas/discovery.py                       # 新增
tests/integration/test_discovery_api.py             # 新增
```

---

#### PR 5.2: Discovery Rerun API

##### 范围

- [x] `POST /api/discovery/rerun` → 异步 task_id + audit log

##### 建议文件

```text
apps/api/routers/discovery.py                       # 修改
tests/integration/test_discovery_rerun_api.py       # 新增
```

---

#### PR 5.3: Config Publish / Rollback / Revoke API

##### 范围

- [x] `POST /api/config/publish`, `POST /api/config/rollback`, `POST /api/config/revoke`
- [x] `GET /api/config/current`, `GET /api/config/versions`
- [x] 所有操作写入 audit log

##### 建议文件

```text
apps/api/routers/config.py                          # 新增
apps/api/schemas/config.py                          # 新增
tests/integration/test_config_api.py                # 新增
```

---

#### PR 5.4: Override API

##### 范围

- [x] `POST /api/config/overrides`, `GET /api/config/overrides`, `DELETE /api/config/overrides/{id}`
- [x] Override 必须包含 `reason`

##### 建议文件

```text
apps/api/routers/config.py                          # 修改
tests/integration/test_override_api.py              # 新增
```

---

#### PR 5.5: Worker `_build_deps` 集成

##### 背景

这是连接 discovery + config 系统与现有诊断 workflow 的关键 PR。

##### 范围

- [x] 修改 `_build_deps()` — 生产路径使用 `EffectiveConfig.from_operator_sources()`
- [x] 保留 demo 路径使用 `EffectiveConfig.from_demo_sources()`
- [x] 生产路径：`allow_discovery_proposals=False`
- [x] `detected_only` backend 不构造工具
- [x] 缺失 backend tool 返回 degraded/unavailable
- [x] token/secret 不进入 LLM prompt
- [x] `config_version_id` 写入 agent run state

##### 不做

- 不改变 LangGraph workflow 节点逻辑
- 不改变 executor backend 逻辑

##### 建议文件

```text
apps/worker/tasks.py                                # 修改：_build_deps 重构
tests/unit/test_build_deps_integration.py           # 新增
tests/integration/test_worker_with_effective_config.py # 新增
```

##### 测试清单

```text
test_worker_uses_published_config
test_worker_does_not_use_proposal
test_detected_only_backend_not_constructed
test_missing_backend_tool_degraded
test_token_not_in_llm_prompt
test_config_version_id_in_run_state
test_demo_path_unchanged
test_production_missing_prometheus_degraded
```

##### 验收标准

- [ ] 生产 worker 只使用 published config + env/profile
- [ ] 未发布 proposal 不进入诊断路径
- [ ] Token/secret 不进入 LLM context
- [ ] Fixture/demo 路径不受影响
- [ ] 现有所有测试通过

##### 风险点

- 这是最核心的集成点，修改 `_build_deps()` 可能影响所有诊断流程
- MetricsTool 需要在 `base_url=None` 时优雅降级

##### 回滚方案

- 恢复 `_build_deps()` 到修改前版本（直接使用 settings）

---

### M6: Runbook Template Generation

**目标：** 实现确定性 Runbook 模板生成（Jinja2），不调用 LLM，不调用 web_search。

**前置依赖：** M3（DiscoveryResult 提供能力矩阵、指标映射、拓扑）

---

#### PR 6.1: RunbookTemplateEngine

##### 范围

- [x] 实现 `RunbookTemplateEngine` 类（Jinja2 渲染）
- [x] 5 个初始模板
- [x] 能力矩阵驱动段落可见性
- [x] 不引用不存在的 metric

##### 建议文件

```text
packages/discovery/runbook_template_engine.py       # 新增
packages/discovery/templates/*.md.j2                # 新增目录 + 5 个模板
tests/unit/test_runbook_template_engine.py          # 新增
```

##### 测试清单

```text
test_render_db_connection_template
test_missing_metric_hides_metric_step
test_has_k8s_includes_k8s_step
test_has_traces_includes_trace_step
test_no_k8s_hides_k8s_step
test_template_does_not_invent_metrics
```

---

#### PR 6.2: RunbookDraft 扩展与 Ingest

##### 范围

- [x] 扩展 `RunbookDraft` 模型：添加 `draft_type`, `source`, `discovery_run_id`
- [x] 实现 approved draft → `RunbookVersion` → `runbook_chunks` ingest
- [x] Pending/rejected draft 不能进入 `runbook_chunks`
- [x] `RunbookChunk.source_path` 强制填写

##### 建议文件

```text
packages/db/models.py                               # 修改：扩展 RunbookDraft
migrations/versions/XXXX_runbook_draft_type.py      # 新增
packages/rag/ingest.py                              # 修改
tests/unit/test_runbook_draft_ingest.py             # 新增
```

##### 测试清单

```text
test_create_template_draft
test_draft_default_pending_review
test_rejected_draft_not_ingested
test_approved_draft_ingested
test_pending_draft_not_ingested
test_chunk_has_source_path
test_ingest_creates_version_record
```

---

#### PR 6.3: Runbook Review API

##### 范围

- [x] `GET /api/runbooks/drafts` + detail + `POST .../review` + `POST /api/runbooks/regenerate`

##### 建议文件

```text
apps/api/routers/runbooks.py                        # 修改
tests/integration/test_runbook_review_api.py        # 新增
```

---

### M7: Deterministic Runbook Feedback

**目标：** 实现确定性反馈收集，不调用 LLM/web_search。

**前置依赖：** M6

---

#### PR 7.1: Incident Aggregation

##### 范围

- [x] 聚合同 service + fault_type 的 incident
- [x] 触发条件：>= `RUNBOOK_AMENDMENT_MIN_INCIDENTS` 次

##### 建议文件

```text
packages/discovery/runbook_feedback.py              # 新增
tests/unit/test_runbook_feedback_aggregation.py     # 新增
```

##### 测试清单

```text
test_less_than_min_incidents_no_feedback
test_min_incidents_generates_summary
```

---

#### PR 7.2: Action Statistics

##### 范围

- [x] 统计成功/失败/跳过/拒绝的动作
- [x] 根因置信度 >= 0.7 才参与

##### 建议文件

```text
packages/discovery/runbook_feedback.py              # 修改
tests/unit/test_runbook_feedback_actions.py         # 新增
```

##### 测试清单

```text
test_successful_actions_collected
test_failed_actions_collected
test_skipped_actions_collected
test_rejected_actions_collected
test_low_confidence_no_feedback
```

---

#### PR 7.3: Gap Detection

##### 范围

- [x] 识别缺失 fault type、diagnostic step、recurring evidence pattern

##### 建议文件

```text
packages/discovery/runbook_feedback.py              # 修改
tests/unit/test_runbook_feedback_gaps.py            # 新增
```

##### 测试清单

```text
test_missing_fault_type_detected
test_missing_diagnostic_step_detected
test_recurring_evidence_pattern_detected
```

---

#### PR 7.4: AmendmentDraft 与频率控制

##### 范围

- [x] 生成 `RunbookFeedbackSummary` + `AmendmentDraft`
- [x] Cooldown 控制（7 天）
- [x] 所有结果进入 review queue
- [x] 不调用 LLM、不调用 web_search、不直接写 runbook_chunks

##### 建议文件

```text
packages/discovery/runbook_feedback.py              # 修改
tests/unit/test_runbook_feedback_analyzer.py        # 新增
```

##### 测试清单

```text
test_cooldown_blocks_repeat
test_create_amendment_draft
test_amendment_draft_pending_review
test_no_llm_called
test_no_web_search_called
test_not_ingested_directly
```

---

### M8: Testing & Docs

**目标：** 补齐测试覆盖、集成测试、生产安全测试、E2E 测试和文档。

**前置依赖：** M0–M7

**不做：** 不引入新功能

---

#### PR 8.1: 单元测试补齐

- [x] MetricMatcher, PromQL Builder, PromQL Validator, LabelDetector, TopologyDeriver, AutomationPolicy, ConfigMerge, Poll Scope, MatcherParser, Poll Cursor, ResolvedInference, Runbook Template, Feedback Analyzer

##### 目标覆盖率：>= 80%

---

#### PR 8.2: 集成测试

- [x] Mock Prometheus/Loki/Fake K8s/Mock Alertmanager HTTP servers
- [x] Postgres test DB + Redis test instance
- [x] DiscoveryRunner 完整流程、Poll task 完整流程

---

#### PR 8.3: 生产安全测试

##### 必须覆盖

```text
test_production_no_published_config
test_production_no_backend_urls
test_production_llm_disabled
test_production_executor_fixture
test_severity_only_poll_disabled
test_allowlist_unmappable_disabled
test_truncation_skips_resolved_inference
test_unpublished_proposal_not_used
test_expired_config_not_used
test_detected_only_backend_not_constructed
test_web_search_default_false
test_runbook_draft_not_ingested
test_executor_live_never_auto_apply
test_l4_direct_reject_preserved
test_token_not_in_llm_context
```

---

#### PR 8.4: E2E 测试

- [x] **E2E 1：** Local demo fixture 不受影响
- [x] **E2E 2：** Discovery → Proposal → Publish → Worker 使用 config
- [x] **E2E 3：** Alertmanager poll → Incident → Dedup → Resolved inference

---

#### PR 8.5: 文档

- [x] `docs/production-checklist.md`
- [x] `docs/discovery.md`
- [x] `docs/alertmanager-poll.md`
- [x] `docs/config-publish-rollback.md`
- [x] `docs/runbook-template.md`
- [x] `docs/degraded-behavior.md`
- [x] `docs/security-boundaries.md`
- [x] 更新 `.env.example`

---

### M9+: Future Extensions

**以下仅为后续规划，不进入当前实现。**

| Capability | Default | Constraints |
|-----------|---------|-------------|
| LLM Runbook Generation | `RUNBOOK_LLM_GENERATION_ENABLED=false` | 只能生成 draft，不能直接发布 |
| Runbook web_search | `RUNBOOK_WEB_SEARCH_ENABLED=false` | SSRF 防护、脱敏、来源追溯 |
| LLM incident diff analysis | disabled | 只能生成 AmendmentDraft |
| TempoTraceBackend | `TRACE_BACKEND=tempo` | Phase 9+ 实现 |
| Grafana webhook parser | — | `_from_grafana_alert()` 已存在但需增强 |
| Tempo auto-discovery enablement | status=detected_only | TempoTraceBackend 实现后启用 |
| Semantic runbook search | — | 依赖 embedding provider |
| External embedding provider | — | 可选替代 BGE-ZH |

---

## 5. Dependency Graph

```
M0 (Production Safety Foundation)
 ├──> M1 (Prometheus Core Discovery)
 │     └──> M3 (DiscoveryRunner + Config)
 │           ├──> M5 (Discovery API / Worker Integration)
 │           ├──> M6 (Runbook Template Generation)
 │           │     └──> M7 (Runbook Feedback)
 │           └──> (M5 + M6 + M7) ──> M8 (Testing & Docs)
 ├──> M2 (K8s / Loki / Topology)
 │     └──> M3 [...]
 ├──> M4 (Alertmanager Poll)
 │     └──> M5 [...]
 └──> M8 [...]

M8 ──> M9+ (Future)
```

**并行化说明：**

| 可并行组 | 并行条件 |
|---------|---------|
| M1 + M2 + M4 | M0 完成后，三个 milestone 互不依赖 |
| M1 内部 PR 1.1 → 1.2 → (1.3 + 1.4 + 1.5) → 1.6 | 1.3/1.4/1.5 可并行 |
| M2 内部 PR 2.1 → (2.2 + 2.3 + 2.4 + 2.5) | 2.2/2.3/2.4/2.5 可并行 |
| M0 内部 PR 0.1 → (0.2 + 0.3) → 0.4 → 0.5 | 0.2 和 0.3 可并行 |
| M8 内部 PR 8.1 + 8.2 + 8.3 + 8.5 | 全部可并行 |

**阻塞项：**
- M0.1 (settings) 是所有后续工作的硬阻塞
- M1 + M2 是 M3 的硬阻塞
- M3 是 M5/M6 的硬阻塞
- M6 是 M7 的硬阻塞

---

## 6. Suggested First 10 Issues

| # | Title | Goal | Files | Tests | Depends On | Parallel? | Risk | TDD First? |
|---|-------|------|-------|-------|-----------|-----------|------|------------|
| 1 | **Settings 生产默认值** | 添加 APP_ENV + 安全默认值 | `packages/common/settings.py` | `test_settings_production_defaults.py` | none | no | MEDIUM | yes |
| 2 | **Discovery/Config/Audit DB 迁移** | 创建 4 个新表 + 扩展 AuditLog | `packages/db/models.py`, `migrations/` | `test_discovery_models.py` | #1 | with #3 | LOW | yes |
| 3 | **AutomationPolicy** | 实现自动发布/审核/拒绝判定 | `packages/discovery/automation_policy.py` | `test_automation_policy.py` | #1 | with #2 | HIGH | yes |
| 4 | **EffectiveConfig 读取链路** | 实现配置优先级合并 | `packages/discovery/config_merge.py` | `test_config_merge.py` | #1, #2 | no | HIGH | yes |
| 5 | **PrometheusClient** | HTTP 客户端封装 6 个 Prometheus API | `packages/discovery/prom_discovery.py` | `test_prometheus_client.py` | #1 | with #8, #9, #10 | MEDIUM | no |
| 6 | **MetricCandidate 模板库** | 实现 5 种语义指标的正则模板 | `packages/discovery/models.py` | `test_discovery_models_validation.py` | #1 | with #5 | LOW | yes |
| 7 | **MetricMatcher** | 语义匹配引擎 | `packages/discovery/metric_matcher.py` | `test_metric_matcher.py` | #5, #6 | no | HIGH | yes |
| 8 | **PromQL Builder + Validator** | PromQL 生成 + dry-run | `packages/discovery/promql_builder.py`, `promql_validator.py` | `test_promql_builder.py`, `test_promql_validator.py` | #5 | with #7 | MEDIUM | yes |
| 9 | **K8sDiscovery** | K8s API 服务发现 | `packages/discovery/k8s_discovery.py` | `test_k8s_discovery.py` | #1 | with #5, #10 | MEDIUM | no |
| 10 | **Alertmanager Matcher Parser** | Matcher 表达式解析 | `packages/discovery/matcher_parser.py` | `test_matcher_parser.py` | #1 | with #5, #9 | LOW | yes |

---

## 7. Parallelization Plan

### Phase 1 (Week 1): Foundation
- **Day 1-2:** Issue #1 (Settings) — 单人，串行
- **Day 2-3:** Issue #2 (DB 迁移) + Issue #3 (AutomationPolicy) — 可并行（2 人）
- **Day 3-4:** Issue #4 (EffectiveConfig) — 依赖 #2

### Phase 2 (Week 2): Core Discovery
- **Day 5-6:** Issue #5 (PrometheusClient) + Issue #6 (MetricCandidate) — 可并行
- **Day 6-7:** Issue #7 (MetricMatcher) — 依赖 #5, #6
- **Day 7-8:** Issue #8 (PromQL Builder) + Issue #9 (K8sDiscovery) — 可并行

### Phase 3 (Week 3): Extended Discovery
- **Day 9-10:** LabelDetector + LokiDiscovery + WorkloadBinding + ServiceEdge — 可并行
- **Day 10-11:** DiscoveryRunner (PR 3.1)
- **Day 11-12:** CostControl + CapabilityAssessor — 可并行

### Phase 4 (Week 4): Config & Poll
- **Day 13-14:** ConfigPublisher (PR 3.6) + AlertmanagerClient (PR 4.1) — 可并行
- **Day 14-15:** Poll Cursor + MatcherParser + ScopeValidation + Allowlist — 部分并行
- **Day 15:** Poll Task 集成

### Phase 5 (Week 5): API & Worker Integration
- **Day 16-17:** Discovery Read API + Config API + Override API — 可并行
- **Day 18:** Worker _build_deps 集成 — 串行，关键 PR

### Phase 6 (Week 6-7): Runbook
- **Day 19-20:** RunbookTemplateEngine
- **Day 21:** RunbookDraft 扩展 + Review API
- **Day 22-23:** RunbookFeedback (4 个 PR，可部分并行)

### Phase 7 (Week 8): Testing
- **Day 24-25:** 所有 M8 PR 可并行（测试 + 文档）

---

## 8. Risk Register

| Risk | Area | Impact | Probability | Mitigation | Test Coverage |
|------|------|--------|-------------|------------|---------------|
| 生产误用 localhost | Settings | HIGH | MEDIUM | `APP_ENV=production` 时 `_resolve_backend()` 不返回 localhost；测试覆盖 | `test_production_rejects_implicit_localhost_backend` |
| 未发布 proposal 进入 worker | Config | CRITICAL | MEDIUM | `_build_deps()` 生产路径 `allow_discovery_proposals=False`；审计 | `test_worker_does_not_use_proposal` |
| 低置信 metric mapping 自动发布 | AutomationPolicy | HIGH | MEDIUM | `confidence >= 0.90` + metadata 通过 + dry-run 通过；多位校验 | `test_metric_mapping_metadata_missing_not_auto_apply` |
| Alertmanager poll 拉全量 | Poll | HIGH | HIGH | server-side filter[] 优先；allowlist 无法映射时 disabled | `test_unmappable_allowlist_production_disabled` |
| severity-only 误作为有效 scope | Poll | HIGH | MEDIUM | `has_valid_scope()` 排除 severity-only | `test_severity_only_not_valid_scope` |
| truncation 导致误判 resolved | Poll | HIGH | LOW | truncation 时禁止 resolved inference | `test_truncation_blocks_resolved_inference` |
| already_seen_active 不更新 seen 状态 | Poll | MEDIUM | LOW | 函数设计保证副作用（即使返回 True 也更新） | `test_already_seen_updates_last_seen` |
| Service selector 被误用为 ServiceEdge | Topology | MEDIUM | LOW | 代码层面分离 WorkloadBinding 和 ServiceEdge | `test_service_selector_never_creates_service_edge` |
| Runbook draft 未审核进入 runbook_chunks | Runbook | HIGH | LOW | Ingest 前检查 draft status | `test_pending_draft_not_ingested` |
| LLM/web_search 越过审批边界 | Security | CRITICAL | LOW | Phase 0-8 不实现 LLM/web_search；Phase 9+ 默认关闭 | `test_no_llm_called`, `test_no_web_search_called` |
| Discovery 对监控系统造成压力 | Performance | MEDIUM | MEDIUM | 成本控制（timeout, 缓存, 上限, 采样） | `test_metric_names_truncated_with_warning` |
| 配置变更无审计 | Audit | HIGH | LOW | 所有 config 操作写入不可变 audit log | `test_publish_config_creates_audit_log` |
| 旧配置长期生效 | Config | MEDIUM | LOW | 版本默认 30 天过期，过期前 7 天 warning | `test_expired_config_not_used` |
| Token/secret 泄露进入 LLM | Security | CRITICAL | LOW | Redaction before LLM call；secret 字段用 SecretStr | `test_token_not_in_llm_context` |
| Worker _build_deps 重构破坏现有流程 | Integration | HIGH | MEDIUM | 保留 demo 路径；先写测试再重构 | `test_demo_path_unchanged` |

---

## 9. Test Strategy

### 9.1 测试金字塔

```
        /\
       /E2E\        3 条 E2E 测试（mock 环境）
      /------\
     /Integration\   20+ 集成测试（mock HTTP servers + real DB/Redis）
    /------------\
   /   Unit Tests  \  100+ 单元测试（纯函数、mock 依赖）
  /----------------\
```

### 9.2 测试分层

| Layer | Scope | Tools | Coverage Target |
|-------|-------|-------|----------------|
| Unit | 纯函数、Pydantic 模型、策略逻辑 | pytest + unittest.mock | >= 80% |
| Integration | DB 操作、HTTP 客户端、Redis 锁、Celery tasks | pytest + SQLite + fakeredis + httpx mock | 关键路径 100% |
| Production Safety | 生产默认值、安全边界、降级行为 | pytest + Settings override | 100% of safety boundaries |
| E2E | 完整流程（discovery → diagnosis → report） | pytest + mock servers | 3 条主流程 |

### 9.3 关键测试原则

1. 所有测试使用 FakeLLM — CI 不调用真实 LLM
2. 所有测试使用 fixture 默认值 — 不依赖外部服务
3. 生产安全测试必须有 — 每个安全边界至少一个测试
4. DB 测试使用 SQLite in-memory — 快速、隔离
5. Mock HTTP server 用于集成测试 — 使用 `pytest-httpx`
6. Redis 测试使用 `fakeredis` — 不需要真实 Redis 实例

---

## 10. Definition of Done

### 全局 DoD

- [ ] 所有新增逻辑有单元测试
- [ ] 关键生产路径有集成测试
- [ ] 生产默认安全：`LLM_PROVIDER=disabled`, `EXECUTOR_BACKEND=fixture`
- [ ] 失败 degraded 而不是 panic
- [ ] 配置变更有 audit log
- [ ] Worker 只使用 published config
- [ ] Fixture/demo 不受影响
- [ ] CI 使用 FakeLLM
- [ ] 没有 Phase 9+ 功能混入 Phase 0-8
- [ ] 文档和 `.env.example` 更新
- [ ] `ruff check` + `mypy` 无错误
- [ ] `pytest` 全部通过
- [ ] 覆盖率 >= 80%
- [ ] 所有 PR 经过 code review

### 每个 PR 的 DoD

- [ ] 代码通过 lint + type check
- [ ] 全部新增测试通过
- [ ] 全部已有测试通过
- [ ] 测试覆盖率 >= 80%（对新增代码）
- [ ] PR 描述清晰（背景、范围、不做、设计要点）
- [ ] 回滚方案明确
- [ ] 不引入安全漏洞

---

## 11. Open Questions

| Question | Why It Matters | Recommended Default | Risk If Wrong |
|----------|----------------|---------------------|---------------|
| `DiscoveryRun.result_json` 存储大小限制？ | 完整 DiscoveryResult 可能很大（几百 KB 到 MB） | JSONB 列，建议限制 10MB；超大结果截断 metadata 细节 | 存储膨胀，查询变慢 |
| Alertmanager poll cursor 存储在 DB 还是 Redis？ | Cursor 需要跨 poll 轮次持久化，也需要快速读写 | DB 持久化（PostgreSQL），Redis 缓存加速 `already_seen_active` 查询 | DB 压力过大（高频 poll）；纯 Redis 丢失 cursor 后无法正确推断 resolved |
| `_build_deps()` 中 `effective.prometheus_url = None` 时 MetricsTool 如何降级？ | 这是最核心的降级路径 | MetricsTool 初始化时 `base_url=None` 则所有操作返回 `ToolResult(status="degraded")` | 如果抛异常会导致整个 worker 失败 |
| M5 的配置 API 是否需要新的权限模型？ | 配置发布/回滚是敏感操作 | 复用现有 API key auth + `operator` role annotation；Phase 0-8 不做 RBAC 细化 | 权限过宽可能被误操作；权限过严阻碍正常使用 |
| PromQL dry-run 方式选 A（query_range）还是 B（query + range selector）？ | 两种方式对 Prometheus 的负载不同 | 方式 B（query + range selector 变体），与现有 `MetricsTool._query_shard` 模式一致 | `query_range` 可能更准确但与现有代码模式不一致 |
| `SEMANTIC_PATTERNS` 模板库是否应该让 operator 扩展？ | 不同环境可能使用完全不同的指标名 | Phase 0-8 内置模板 + profile override（JSON/YAML 文件）；Phase 9+ 可由 LLM 辅助扩展 | 内置模板覆盖不足导致大量 `unavailable`；但允许随意扩展可能导致误匹配 |
| Jaeger trace call graph 在 Phase 2 中的实现深度？ | 完整实现需要多 trace 聚合、方向冲突处理 | Phase 2 只做基本提取（1:1 service → downstream mapping），复杂聚合延后到 Phase 9+ | 基本实现可能产生不准确的拓扑，但比没有好 |
| 现有 `RunbookDraft` 模型的 `status` 字段是否够用？ | 模板 draft 和 LLM draft 的生命周期不同 | 扩展 `draft_type` 区分，保留 `status` 字段（`pending_review/reviewed/rejected`） | 如果混用会导致 LLM draft 被模板流程错误 published |
| K8s discovery 的 K8s Python client 依赖如何处理？ | `kubernetes` 包是可选依赖 | 懒加载（`LiveK8sBackend` 仅在调用 `fetch()` 时 import），`k8s_backend=fixture` 时不需要 | 强制依赖会增加安装复杂度 |
| Celery Beat 调度是否应该与 worker 进程分离？ | Beat 调度器与 worker 同进程可能在生产中有问题 | Phase 0-8 同进程（简化部署）；生产建议分离（文档说明） | 同进程可能导致调度延迟（worker 忙时） |
| `alert_poll_lock_key` 是否应包含 `namespace_allowlist` 和 `service_allowlist`？ | 如果不同 allowlist 使用相同 lock_key，可能互相阻塞 | 包含 `audit_safe_dict()` 的 hash；allowlist 转 matcher 逻辑早于 lock_key 生成 | 如果不包含，不同范围过滤可能错误共享锁 |
