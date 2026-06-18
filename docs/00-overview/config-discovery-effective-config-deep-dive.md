# 配置、Discovery 与 EffectiveConfig 技术深挖

**最后更新：** 2026-06-18

本文沿当前代码路径说明 `Settings`、M9 feature gates、Discovery、Config API、Override、`EffectiveConfig` 和 worker `_build_deps()` 如何组合。它补充 [配置参考](../11-reference/configuration.md) 和 [后端对接范围](../11-reference/backend-connectivity.md)：配置参考列字段，本文解释运行时读写边界和排障路径。DiscoveryRunner、capability matrix、topology、manual rerun lock 和 proposal 细节见 [Discovery、Capability Matrix 与服务拓扑技术深挖](discovery-capability-topology-deep-dive.md)。

## 阅读目标

读完本文应能回答：

- `.env`、环境变量、生产安全默认值和 M9 feature gate 的生效顺序是什么。
- Discovery run 会写哪些表，是否会直接影响 worker。
- `DiscoveryProposal`、`EffectiveConfigVersion`、`DiscoveryOverride` 的职责边界是什么。
- 生产 worker 实际读取哪些配置源，哪些不会被读取。
- config publish、rollback、revoke 和 override 的 API 行为是什么。
- 后端 URL safety 当前在哪些路径执行，哪里仍依赖 `config:write` 权限边界。
- 没有 published config 或 backend URL 时 worker 为什么降级而不是崩溃。

## 代码入口

| 链路 | 当前入口 |
|------|----------|
| Settings 加载 | `packages/common/settings.py` |
| M9 feature gate 解析 | `packages/common/feature_flags.py` |
| URL 安全校验 | `packages/common/backend_url_safety.py` |
| EffectiveConfig 合并 | `packages/discovery/config_merge.py` |
| Discovery 编排 | `packages/discovery/runner.py` |
| Backend endpoint detection | `packages/discovery/backend_endpoints.py` |
| Discovery 结果持久化 | `packages/discovery/store.py` |
| Discovery proposal helper | `packages/discovery/config_proposal.py` |
| Automation policy helper | `packages/discovery/automation_policy.py` |
| Config publish/rollback/revoke | `packages/discovery/config_publisher.py` |
| Config API | `apps/api/routers/config.py` |
| Discovery API | `apps/api/routers/discovery.py` |
| Worker 依赖构造 | `apps/worker/tasks.py` 的 `_build_deps()` |
| Discovery rerun task | `apps/worker/tasks.py` 的 `run_discovery_rerun()` |
| Alertmanager poll config 读取 | `apps/worker/tasks.py` 的 `_poll_alertmanager_logic()` |

## 关键数据对象

| 对象 | 表/模型 | 写入者 | 读取者 | 当前边界 |
|------|---------|--------|--------|----------|
| Discovery run | `discovery_runs` / `DiscoveryRun` | Discovery API、worker task | Discovery API | 保存结果 summary，不是 worker runtime config |
| Discovery proposal | `discovery_proposals` / `DiscoveryProposal` | `run_discovery_rerun()`、`DiscoveryStore` | operator/API 查询、publish 关联字段 | worker 不读取 proposal |
| Effective config version | `effective_config_versions` / `EffectiveConfigVersion` | Config API、`ConfigPublisher` | production worker、Discovery capability API | 只有 `status="published"` 的最新版本进入 worker |
| Discovery override | `discovery_overrides` / `DiscoveryOverride` | Config override API | production worker | active 条件是未 revoke 且未过期 |
| Audit log | `audit_logs` / `AuditLog` | config/discovery/approval 等写路径 | audit/debug API 或 DB 查询 | repository 不提供 update/delete |
| Settings | `Settings` | `.env`、环境变量、内置默认值 | API、worker、tools | 进程启动/调用时读取，不等同 published config |
| EffectiveConfig | dataclass | `config_merge.py` | worker `_build_deps()`、poll task | 运行时合并结果，不单独落表 |

## 总链路

```text
Settings(.env/env/defaults)
  -> production safety defaults
  -> M9 feature flag resolution

Discovery API / Celery task
  -> DiscoveryRunner
  -> discovery_runs summary
  -> discovery_proposals pending_review

Config API
  -> EffectiveConfigVersion published/superseded/rolled_back/revoked
  -> DiscoveryOverride active/revoked/expired

Worker _build_deps()
  -> if production: Settings + active overrides + latest published config + safe defaults
  -> if local/demo: Settings defaults
  -> AgentDeps tools, LLM, executor, memory, RAG
```

Discovery 发现结果不会自动变成 worker runtime config。当前 worker 只通过 `EffectiveConfigVersion(status="published")` 和 active overrides 接收 operator 批准后的配置。

## Settings 与生产安全默认值

`Settings` 使用 `pydantic-settings` 从 `.env`、环境变量和字段默认值加载。环境变量名是字段名的大写形式，例如 `database_url` 对应 `DATABASE_URL`。

当前 `Settings._apply_production_safety_defaults()` 只在 `APP_ENV=production` 且字段未显式设置时改写两项：

| 字段 | production 未显式设置时 |
|------|--------------------------|
| `LLM_PROVIDER` | `disabled` |
| `DISCOVERY_ENABLED` | `false` |

它不会自动改写 `EXECUTOR_BACKEND`。生产安全依赖默认值仍是 `fixture`，但发布前必须显式确认没有设置成 `live`。

本地和 demo 的默认值保留 localhost、fixture backend、FakeLLM 和 fake embedding，便于一键 demo 和确定性测试。

## M9 Feature Gate

`resolve_m9_feature_flags(settings)` 是 M9 开关解析入口：

- `M9_EXTENSIONS_ENABLED=false` 会强制所有 M9 子能力解析为关闭。
- 子开关打开但 global gate 关闭时，记录 conflict warning 和 Prometheus conflict metric，但不阻断启动。
- 特殊规则：`TRACE_BACKEND=jaeger` 是 M8 已验证路径，M9 关闭时仍可保留；`TRACE_BACKEND=tempo` 是 M9 能力，M9 关闭时标记 degraded。

常见误区：

- 不要直接把 `settings.runbook_web_search_enabled` 当作最终生效状态；M9 子能力应看 resolved flags 或 `is_m9_subfeature_enabled()`。
- Jaeger 与 Tempo 的 gate 行为不同。

## EffectiveConfig 合并

`EffectiveConfig.from_operator_sources()` 支持的优先级是：

```text
env > active override > profile > published EffectiveConfigVersion > safe default
```

当前 worker `_build_deps()` 在 `APP_ENV=production` 实际传入的是：

```text
Settings
active_overrides=_active_overrides_for_effective_config(db)
published_config=latest EffectiveConfigVersion(status="published")
```

`profile_overrides` 是 helper 支持的参数，但当前 worker 没有传入 profile。因此生产 worker 的实际运行优先级是：

```text
env/settings 差异值 > active override > published EffectiveConfigVersion > safe default
```

### Env 识别方式

`from_operator_sources()` 把 `Settings` 中与 hardcoded local default 不同的 URL 视为 env/operator 值。例如 `PROMETHEUS_URL` 不等于 `http://localhost:9090` 时，Prometheus source 为 `env`。

生产环境如果没有显式配置 backend URL，也没有 active override 或 published config，会返回：

```text
BackendConfig(url=None, source="default", degraded=True)
```

worker 会把这类缺失 backend 构造成 `UnavailableTool`，而不是让真实 tool constructor 崩溃。

### Published Config 形状

published config 支持两种读取形状：

```json
{"prometheus_url": "http://prom:9090"}
```

或：

```json
{"prometheus": {"url": "http://prom:9090"}}
```

当前支持的 backend key 包括 `prometheus`、`loki`、`jaeger`、`tempo`、`alertmanager`。Tempo 是可选 backend；缺失 Tempo URL 不会产生 baseline production warning，只有 `TRACE_BACKEND=tempo` 时 trace tool 构造会需要它。

## Worker 读配置边界

`_build_deps()` 的生产路径：

1. 调用 `EffectiveConfigRepository.get_latest_published()`。
2. 只读取最新 `status="published"` 的 `config_snapshot`。
3. 调用 `_active_overrides_for_effective_config()` 读取未过期、未 revoke 的 overrides。
4. 构造 `EffectiveConfig.from_operator_sources()`。
5. 使用 effective URL 构造 metrics/logs/trace tools。
6. URL 缺失时使用 `UnavailableTool`。
7. 把 `effective_config` 和 `config_version_id` 放进 `AgentDeps`。

明确不会进入 worker 的内容：

- `DiscoveryProposal(status="pending_review")`
- `DiscoveryRun.summary`
- `detected_only` backend endpoint
- revoked config version
- revoked 或 expired override

本地/demo 路径直接使用 `EffectiveConfig.from_demo_sources(settings)`，保持 settings defaults 与 fixture/FakeLLM 路径。

## Tool 构造与降级

| Tool | URL 来源 | 缺失时 |
|------|----------|--------|
| MetricsTool | `effective_config.prometheus.url` | `UnavailableTool("metrics")` |
| LogsTool | `effective_config.loki.url` | `UnavailableTool("logs")` |
| TraceTool Jaeger | `effective_config.jaeger.url` | `UnavailableTool("trace")` |
| TraceTool Tempo | `effective_config.tempo.url` | `UnavailableTool("trace")` |
| Trace fixture in production | 不允许生产 deps 使用 fixture | `UnavailableTool("trace")` |
| Git/K8s/DB diagnostics | 当前仍由 settings/backend factory 构造 | 按各 backend 自身规则降级 |
| Executor | `EXECUTOR_BACKEND` settings | 默认 fixture，live 显式 opt-in |

这意味着 EffectiveConfig 当前主要控制 observability backend URL 和 service label；K8s diagnostics、DB diagnostics、deployment backend 和 executor 仍走 settings。

## Config Publish/Rollback/Revoke

Config API 路径：

| Endpoint | Scope | 行为 |
|----------|-------|------|
| `GET /api/config/current` | `config:read` 或 `config:write` | 返回当前 published config |
| `GET /api/config/versions` | `config:read` 或 `config:write` | 返回版本列表 |
| `POST /api/config/publish` | `config:write` | 创建新 `EffectiveConfigVersion`，旧 published 置为 superseded |
| `POST /api/config/rollback` | `config:write` | 当前 published 置为 rolled_back，恢复最近 superseded 版本 |
| `POST /api/config/revoke` | `config:write` | 将版本置为 revoked |

`ConfigPublisher.publish()` 会：

- 计算递增 `version_number`。
- 将旧 latest published 标记为 `superseded`。
- 创建新 `status="published"` 版本。
- 设置 `stale_warning_at`，默认 30 天后提示 stale。
- 写 `config.publish` audit log。

当前实现差异需要注意：

- `POST /api/config/publish` 直接接受 `config_snapshot`，`proposal_id` 是可选关联字段，不要求必须来自 proposal。
- `ConfigPublisher.publish()` 当前不递归校验 `config_snapshot` 里的所有 URL 字段。它是高权限 `config:write` 路径，必须严格控制 scope；如果要把 publish 暴露给不完全可信输入，应先补 URL safety 校验和测试。
- stale config 仍会被 worker 使用；stale 是 warning，不是硬过期。

## Override 生命周期

Override API 路径：

| Endpoint | Scope | 行为 |
|----------|-------|------|
| `GET /api/config/overrides` | `config:read` 或 `config:write` | 返回 active overrides |
| `POST /api/config/overrides` | `config:write` | 创建带 TTL 的 override |
| `DELETE /api/config/overrides/{override_id}` | `config:write` | 标记 revoked |

active override 条件：

```text
revoked_at IS NULL AND expires_at > now
```

默认 TTL：

| backend_type | 默认 TTL |
|--------------|----------|
| `prometheus` | 7 天 |
| `loki` | 7 天 |
| `jaeger` | 7 天 |
| `alertmanager` | 7 天 |
| 其它 | 14 天 |

最大 TTL 是 30 天。`override_json` 中如果包含 `url`，API 会用 `BackendUrlSafetyValidator` 校验。通用 override API 禁止 secret/auth/executor/live 等字段，例如 `bearer_token`、`password`、`executor_backend`、`live`。

当前 worker 只从 active override 中提取：

```text
backend_type
override_json.url
override_json.auth_type 或 "none"
```

没有 `url` 的 override 不参与 EffectiveConfig 合并。

## Discovery Rerun

Manual rerun API：

```text
POST /api/discovery/rerun
```

需要 `discovery:write` scope。当前流程：

1. API 创建 `DiscoveryRun(source="manual_rerun", trigger_type="manual")`。
2. 尝试获取 Redis lock `discovery:runner`。
3. 入队 `run_discovery_rerun(discovery_run_id)`。
4. 写 `discovery.rerun_requested` audit log。
5. worker 构造 `DiscoveryRunner`，执行 K8s、backend endpoint、Prometheus、Loki、Jaeger、topology、capability matrix。
6. `DiscoveryStore.finish_run()` 把 summary 写入 `discovery_runs`。
7. 如果有 backend endpoints 或 metric mappings，创建 `DiscoveryProposal(status="pending_review")`。
8. 写 `discovery.rerun_complete` audit log。

当前 `run_discovery_rerun()` 使用 `_result_to_config_diff(result)` 直接生成 proposal diff。`ConfigProposalGenerator` 和 `AutomationPolicy` 已存在并有测试，但当前 rerun task 没有把 `ready_to_publish` 接到 `ConfigPublisher.publish()`。因此当前 discovery rerun 不会自动发布 runtime config。

当前 `DISCOVERY_MANUAL_RERUN_ENABLED` 只是 settings 字段，router 没有显式检查它；manual rerun 的实际控制是 `discovery:write` scope、Redis TTL lock 和 Celery enqueue。成功入队后 manual lock 当前依赖 300 秒 TTL 过期。

## Auto Discovery

`auto_discovery_rerun()` 是周期性 discovery task，当前条件：

- `DISCOVERY_ENABLED=true`
- `K8S_BACKEND=live`
- 能获取 Redis lock `lock:discovery:auto`

它创建 `DiscoveryRun(source="auto_periodic")`，执行 discovery，写 run summary 和 audit。当前 auto discovery task 不生成 config proposal，也不 publish config。

生产环境 `DISCOVERY_ENABLED` 未显式设置时默认为 false。启用生产 auto discovery 前，应先确认 URL safety、scope、published config 和 override TTL 流程都已通过。

## Backend Endpoint Detection

`BackendEndpointDetector` 从 K8s service/endpoints/ingress 中识别 Prometheus、Loki、Jaeger、Alertmanager、Tempo。核心规则：

- 已有 manual URL 的 backend 不会被 discovery 替换。
- Tempo discovery 需要 M9 `tempo_discovery` 子能力有效开启。
- 生产环境候选 backend URL 最高状态为 `requires_review`，不会是 `ready`。
- 多候选、低信心、auth unknown 都会要求 review 或 detected-only。
- URL safety 不通过时，普通 backend 降级，Tempo 会拒绝。

状态含义：

| Status | 含义 |
|--------|------|
| `ready` | local 且单候选、auth 已知、信心足够 |
| `requires_review` | 需要人工 review，生产 backend URL discovery 默认走这里 |
| `detected_only` | 信心不足或只能作为发现记录 |
| `degraded` | 发现或 URL safety 有问题 |
| `unavailable` | 未找到或 backend 不可用 |
| `rejected` | 安全检查拒绝，Tempo 等路径会用 |

无论状态如何，endpoint detection 结果都只是 discovery result/proposal 证据；没有 publish 前不会进入 worker。

## URL Safety

`BackendUrlSafetyValidator` 的生产默认会拒绝：

- 非 `http`/`https` scheme。
- URL 中的 username/password。
- localhost、loopback、link-local、metadata endpoint。
- 私网 IP，除非 allowlist。
- 可选严格模式下的 cluster internal domain、非 HTTPS、DNS 解析到危险 IP。

当前执行路径：

| 路径 | 是否调用 URL safety |
|------|---------------------|
| Override API 的 `override_json.url` | 是 |
| Backend endpoint detector | 是 |
| Web/external provider 专用校验 | 是，使用更严格选项 |
| Config publish 任意 `config_snapshot` | 当前不是递归强校验 |
| Worker merge 已发布 config | 当前按已发布内容合并，不重新验证 |

因此 `config:write` 是强权限，应只授予可信 operator/service。若后续加强 publish/worker merge URL validation，需要同步更新 `docs/11-reference/configuration.md`、本文件和 `test_config_api.py`、`test_worker_with_effective_config.py` 等测试。

## Alertmanager Poll 与 EffectiveConfig

`_poll_alertmanager_logic()` 也使用 `EffectiveConfig.from_operator_sources()` 获取 Alertmanager URL：

- 读取 latest published config。
- 叠加 active overrides。
- 使用 `effective_config.alertmanager.url` 构造 `AlertmanagerClient`。
- 如果没有 URL，返回 degraded，不抓取告警。
- namespace/service allowlist 会使用 `effective_config.metrics_service_label` 映射 server-side matchers。

这意味着生产 poll 与 worker diagnosis 一样，不会读取未发布 proposal。

## 当前实现差异与不要误读

- `EffectiveConfig.from_operator_sources()` 支持 profile overrides，但当前 worker 没有接入 profile 参数。
- `AutomationPolicy` 可以计算 auto-apply，但当前 discovery rerun task 不会自动 publish。
- `ConfigProposalGenerator` 可生成结构化 proposal item，但当前 worker rerun 使用 `_result_to_config_diff()` 的简化 diff。
- `ConfigPublisher.publish()` 是高权限写路径，当前不递归验证 `config_snapshot` URL。
- `DiscoveryRun.summary` 是展示和审计数据，不是 runtime config。
- `EffectiveConfigVersion.stale_warning_at` 只是 stale warning，不会让 worker 停用 published config。
- `EXECUTOR_BACKEND` 不受 EffectiveConfig 控制，也不会被 production validator 自动改写。

## 调试 Checklist

### Worker 没读到 backend URL

- 确认 `APP_ENV`。local/demo 走 settings defaults，production 走 operator sources。
- 查 `effective_config_versions` 是否有最新 `status="published"` 版本。
- 查 `config_snapshot` 是 flat `prometheus_url` 还是 nested `prometheus.url`，两者都支持。
- 查 active override 是否未过期且未 revoked。
- 看 `agent_runs` 或 worker logs 中的 `config_version_id`。
- 看 tool call/node trace 是否返回 `UnavailableTool` degraded summary。

### Discovery 有结果但诊断仍 degraded

- Discovery result/proposal 不会自动进入 worker。
- 确认是否通过 `POST /api/config/publish` 发布了有效 config snapshot。
- 确认 proposal status 不是误以为 published。
- 生产 backend endpoint discovery 默认 `requires_review`，这不是可执行配置。

### Override 没生效

- 确认 `expires_at > now`。
- 确认 `revoked_at IS NULL`。
- 确认 `override_json.url` 存在且是字符串。
- 当前 worker 只读取 backend URL override，不读取其它任意 override 字段。

### URL 被拒绝

- 生产环境 localhost、metadata endpoint、私网 IP 默认拒绝。
- 内网服务需要 `BACKEND_URL_ALLOWLIST` 或特定 K8s evidence 路径支持。
- URL 中不要放用户名、密码、token；secret/auth 字段也不能通过通用 override API 下发。

### M9 子能力没生效

- 先确认 `M9_EXTENSIONS_ENABLED=true`。
- 再确认对应子开关开启。
- Tempo 需要 M9；Jaeger 不需要。
- 子开关冲突会记录 warning/metric，但不会阻断服务启动。

## 不要破坏的边界

- 不让 `DiscoveryProposal`、`detected_only` 或 `DiscoveryRun.summary` 直接进入 worker runtime config。
- 不让 discovery 在生产环境自动 publish backend URL。
- 不通过通用 override API 设置 secret/auth/executor/live 字段。
- 不把 `EXECUTOR_BACKEND=live` 放进 discovery/config 自动化路径。
- 不把 stale config 当作硬过期。
- 不让 `config:write` 默认包含在普通 admin/read key 中。
- 不把 M9 子能力写成 global gate 关闭时仍可用。
- 不在生产 worker 中使用 trace fixture backend。

## 相关测试入口

- `tests/unit/test_settings_production_defaults.py`
- `tests/unit/test_m9_feature_flags.py`
- `tests/unit/test_config_merge.py`
- `tests/unit/test_config_publisher.py`
- `tests/unit/test_config_proposal.py`
- `tests/unit/test_automation_policy.py`
- `tests/unit/test_backend_url_safety.py`
- `tests/unit/test_production_safety.py`
- `tests/unit/test_discovery_runner.py`
- `tests/unit/test_discovery_store.py`
- `tests/unit/test_discovery_models.py`
- `tests/unit/test_tempo_endpoint_detection.py`
- `tests/integration/test_config_api.py`
- `tests/integration/test_config_api_auth.py`
- `tests/integration/test_override_api.py`
- `tests/integration/test_override_api_auth.py`
- `tests/integration/test_discovery_api.py`
- `tests/integration/test_discovery_api_auth.py`
- `tests/integration/test_discovery_rerun_api.py`
- `tests/integration/test_worker_with_effective_config.py`
- `tests/e2e/test_m9_tempo_grafana.py`
