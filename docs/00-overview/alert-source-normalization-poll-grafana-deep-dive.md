# Alertmanager Poll、Grafana 与告警来源归一化技术深挖

**最后更新：** 2026-06-18

本文按当前代码说明告警如何从不同来源进入统一 incident 创建链路。主链路从告警到报告的完整执行见 [告警到报告技术深挖](alert-to-report-deep-dive.md)；本文聚焦告警入口本身：`POST /api/alerts`、Alertmanager poll、Grafana-shaped payload、fingerprint 去重、poll cursor 和 resolved inference。

## 阅读目标

读完本文后，开发者应能回答：

- 通用 `/api/alerts` 如何同时接受统一 payload 和 provider-shaped payload。
- Alertmanager webhook 与 Alertmanager poll 为什么都归一到 `source=alertmanager`。
- Grafana 当前已实现的是通用入口的 shape parser 和服务层 helper，而不是独立 webhook router。
- fingerprint 去重、NFA suppression、rate limit、Celery 入队的事务边界在哪里。
- Alertmanager poll 如何用 scope、filter hash、Redis lock 和 cursor 控制生产风险。
- poll 模式下 resolved inference 为什么是保守推断，不等同于 Alertmanager `send_resolved` 事件。

## 一句话模型

当前告警入口可以分为三层：

```text
HTTP push:
  POST /api/alerts
    -> AlertCreateRequest.normalize_provider_payload()
    -> AlertService.create_alert()
    -> Incident + AgentRun + Celery task

Alertmanager pull:
  Celery Beat poll_alertmanager
    -> read EffectiveConfig alertmanager URL
    -> AlertmanagerClient.list_alerts()
    -> _from_alertmanager_single_alert()
    -> AlertService.create_alert()
    -> PollCursorRepository + resolved inference

Grafana current wiring:
  POST /api/alerts can infer Grafana-shaped payloads
  AlertService.ingest_grafana_alert() exists as gated helper
  no dedicated Grafana webhook router is registered in apps/api/main.py
```

因此，所有已接线的 incident 创建最终都进入 `AlertService.create_alert()`。它是 fingerprint 去重、NFA suppression、incident/run 创建和诊断任务入队的统一边界。

## 代码入口一览

| 能力 | 主要代码 | 说明 |
|------|----------|------|
| 通用告警路由 | `apps/api/routers/alerts.py` | `POST /api/alerts`，薄 router，负责限流和 service 调用 |
| 告警 service | `apps/api/services/alert_service.py` | 去重、NFA suppression、incident/run 创建、Celery 入队 |
| 告警 schema/parser | `apps/api/schemas/alerts.py` | provider payload 归一化、severity 归一化、fingerprint 生成 |
| Alertmanager poll task | `apps/worker/tasks.py` | Beat task、scope validation、Redis lock、poll loop、resolved inference |
| Alertmanager client | `packages/discovery/alertmanager_client.py` | `GET /api/v2/alerts` 只读客户端 |
| Matcher/scope | `packages/discovery/matcher_parser.py` | matcher parser、bounded scope 检查、allowlist -> `filter[]` |
| Poll cursor | `packages/db/repositories/poll_cursor.py` | `(filter_hash, fingerprint)` cursor、missing rounds |
| Resolved inference | `packages/discovery/resolved_inference.py` | truncation/grace/missing rounds 保守推断 |
| Cursor model | `packages/db/models.py::AlertPollCursor` | DB 持久化 poll 去重和 missing 状态 |

## 1. 通用 `/api/alerts` 路径

`apps/api/routers/alerts.py` 只做入口层职责：

```text
create_alert(request, payload)
  -> build rate-limit key: API key id or client IP
  -> RateLimiter.is_allowed("alerts", identifier)
  -> AlertService.create_alert(payload)
  -> HTTP 202 AlertCreateResponse
```

限流使用 Redis sliding-window helper；Redis 不可用时当前行为是故障开放。错误响应仍由全局错误处理包装为标准错误信封。

`AlertService.create_alert()` 的核心顺序是：

```text
FalsePositivePatternRepository.should_suppress(fingerprint)
-> IncidentRepository.get_open_by_fingerprint(fingerprint)
-> create Incident(inc_*) + AgentRun(run_*)
-> commit
-> enqueue_diagnosis(incident_id, agent_run_id)
-> save celery_task_id + commit
-> best-effort notification enqueue
```

重要边界：

- 如果 open incident 已有相同 fingerprint，直接返回 `deduplicated=true`，不创建新 run。
- 如果 NFA suppression 命中，当前告警仍会创建/去重 incident，但 severity 会降级为 `P4`，不是丢弃。
- incident/run 先提交，再入队 Celery，保证 worker 用另一条 DB connection 能读到记录。
- Celery 入队失败会把 run 和 incident 标记为 `failed`，避免后续相同 fingerprint 一直去重到一个不会诊断的 open incident。
- notification 入队失败不阻断告警摄取。

## 2. Payload 归一化入口

`AlertCreateRequest` 支持两类输入：

1. 已经是统一字段的 payload：包含 `source`、`fingerprint`、`service`、`severity`、`alert_name`、`starts_at` 等核心字段。
2. provider-shaped payload：缺少统一字段，由 `normalize_provider_payload()` 推断来源并转换。

来源推断规则在 `apps/api/schemas/alerts.py::_infer_source()`：

| 识别特征 | 归一来源 |
|----------|----------|
| `event` dict | `pagerduty` |
| `alert_id` 或 `alert_title` | `datadog` |
| `orgId` 或 `ruleUrl` | `grafana` |
| `commonLabels` + `alerts` | `alertmanager` |
| 其他 | `custom` |

支持的 `source` literal 是：

```text
alertmanager, pagerduty, grafana, datadog, custom, mock
```

severity 归一化会把常见 provider 值映射到 P1-P4：

| 输入示例 | 输出 |
|----------|------|
| `p1`、`critical`、`crit`、`page`、`emergency`、`alert` | `P1` |
| `p2`、`error`、`warning`、`warn`、`high` | `P2` |
| `p3`、`medium`、`minor`、`low` | `P3` |
| `p4`、`info`、`informational`、`ok`、`resolved` | `P4` |

统一字段路径会保留原始请求到 `raw_payload`。provider-shaped 路径会把原始 provider payload 放进 `raw_payload`，并输出归一字段。

## 3. Provider 归一化细节

| Provider | 主要输入字段 | service 选择 | fingerprint 规则 |
|----------|--------------|--------------|------------------|
| Alertmanager webhook | `commonLabels`、`groupLabels`、`alerts[0]` | `service`、`job`、`app`，否则 `unknown` | `alerts[0].fingerprint` 优先，否则 label fingerprint，否则 `alertmanager:{service}:{alert_name}` |
| Alertmanager poll single alert | 单个 `/api/v2/alerts` alert | `service`、`job`、`app`，否则 `unknown` | 与 webhook 对同一 alert 保持相同 fingerprint |
| Grafana-shaped payload | `commonLabels`、`alerts[0].labels`、`alerts[0].annotations` | `service`、`job`、`app`，否则 `unknown` | `grafana:{service}:{alert_name}:{stable_labels}` 的 SHA256 前 16 位 |
| PagerDuty | `event` | service labels / payload service | provider key 或 fallback |
| Datadog | `alert_id`、`alert_title`、tags | tags service | provider key 或 fallback |
| Custom | 任意 JSON | 显式字段或 fallback | 显式 fingerprint 或 fallback |

Alertmanager poll 不新增 `alertmanager_poll` source 枚举值；它仍使用 `source=alertmanager`。当前 `_from_alertmanager_single_alert()` 会在 helper 输出里包含：

```json
{
  "ingestion_metadata": {"ingest_mode": "poll"},
  "raw_labels": {}
}
```

但 worker 当前构造 `AlertCreateRequest` 时只传入 `labels`、`annotations` 和 `raw_payload=alert`，没有把这两个 helper 字段单独持久化到统一 schema 字段。排查 poll 来源时应优先看 `raw_payload`、poll cursor、audit log、Celery task，而不是假设 incident payload 中一定有 `ingestion_metadata`。

## 4. Grafana 当前接线边界

当前代码中有两种 Grafana 相关能力，不能混为一谈。

### 通用 `/api/alerts` 已接线

通用 alert schema 可以识别 Grafana-shaped payload：

```text
POST /api/alerts
  -> _infer_source() returns "grafana"
  -> _from_grafana()
  -> stable Grafana fingerprint
  -> AlertService.create_alert()
```

`tests/e2e/test_m9_tempo_grafana.py` 当前也请求 `/api/alerts`，验证 Grafana-shaped payload 不返回 5xx、resolved/firing 可接受、相同 fingerprint dedup 不崩溃。

Grafana fingerprint 会排除容易变化的 UI/内部字段：

```text
dashboardurl, panelurl, ruleuid, generatorurl,
alert_format, internal_marker, fingerprint
```

因此 dashboard URL、panel URL、rule UID、generator URL 变化不会让相同告警变成不同 incident。

### Grafana helper 存在但未暴露成独立 router

`AlertService.ingest_grafana_alert()` 当前存在，行为是：

```text
if GRAFANA_ALERT_INGEST_ENABLED=false:
    increment grafana_webhook_ignored_total(reason="disabled")
    return None

validate raw_payload is dict
validate raw_payload["alerts"] is list
grafana_to_alert(raw_payload)
AlertCreateRequest(...)
create_alert()
increment grafana_webhook_ingest_total(status="success")
```

但是 `apps/api/main.py` 当前只注册 `alerts`、`incidents`、`agent_runs`、`runbooks`、`reports`、`approvals`、`actions`、`comments`、`approval_groups`、`api_keys`、`config`、`discovery`、`evals` 和 WebSocket router，没有注册独立 Grafana webhook router。也就是说，`GRAFANA_ALERT_INGEST_ENABLED` 控制的是服务层 helper 的行为；当前公开 HTTP 面仍是通用 `/api/alerts`。

还有一个实现细节：`ingest_grafana_alert()` 当前直接检查 `settings.grafana_alert_ingest_enabled`，没有调用 `is_m9_subfeature_enabled(settings, "grafana_alert_ingest")` 叠加 `M9_EXTENSIONS_ENABLED`。由于 helper 当前未暴露为 HTTP route，这不会形成公开入口；如果未来新增独立 Grafana route，应同时接入 M9 feature flag resolver、HMAC、payload size limit 和通用 rate limit。

### Secret 和 size 配置的当前状态

`Settings` 中存在：

```text
GRAFANA_WEBHOOK_SECRET_REF
GRAFANA_WEBHOOK_MAX_BYTES
GRAFANA_ALERT_INGEST_ENABLED
```

当前代码没有把 `GRAFANA_WEBHOOK_SECRET_REF` 或 `GRAFANA_WEBHOOK_MAX_BYTES` 接到一个已注册的 Grafana webhook route 上做 HMAC 或 payload size 校验。文档或上线 checklist 如果要求“启用 Grafana helper 时必须验证 HMAC/size”，应理解为未来独立 webhook route 的安全门槛；它不是当前已暴露 API 的事实。

当前事实是：

- `/api/alerts` 可接受 Grafana-shaped payload，并走通用 API key、request ID、rate limit、schema validation 和 incident dedup。
- `AlertService.ingest_grafana_alert()` 是未暴露的 gated helper。
- Grafana helper metrics 已定义：`agentp_grafana_webhook_ingest_total`、`agentp_grafana_webhook_ignored_total`。
- HMAC/size 配置字段存在，但当前未被公开 route 使用。

## 5. Alertmanager Poll 触发条件

Celery Beat 在 `apps/worker/celery_app.py` 中配置了 `poll-alertmanager`：

```text
task: apps.worker.tasks.poll_alertmanager
schedule: ALERT_POLL_INTERVAL_SECONDS
```

task 入口首先检查：

```text
if ALERT_SOURCE not in ("poll", "both"):
    return skipped

filters = _get_poll_filters(settings)
if not has_valid_scope(filters):
    return skipped

lock_key = lock:poll:alertmanager:{filter_hash}
if RedisLock not acquired:
    return locked
```

因此 `ALERT_SOURCE=webhook` 默认不会 poll。生产启用 poll 或 both 前，必须配置有效 scope，避免全量拉取 Alertmanager active alerts。

## 6. Poll Scope 和 Filter Hash

`_get_poll_filters()` 从 settings 构造 `AlertPollFilters`：

| 配置 | 进入 filter 的方式 |
|------|-------------------|
| `ALERT_POLL_RECEIVER_FILTER` | `receiver` |
| `ALERT_POLL_NAMESPACE_ALLOWLIST` | `namespace_allowlist`，逗号分隔 |
| `ALERT_POLL_SERVICE_ALLOWLIST` | `service_allowlist`，逗号分隔 |
| `ALERT_POLL_FILTER_MATCHERS` | `extra_matchers`，逗号分隔 |

`has_valid_scope()` 要求至少一个有效范围约束：

- receiver；
- namespace allowlist；
- service allowlist；
- 非 `severity` / `priority` 的 extra matcher。

只有 `severity=critical` 或 `priority=page` 这类 matcher 不算有效 scope，因为它们不能限制服务/命名空间/接收范围，容易导致生产全量 poll。

`_build_filter_hash()` 用 receiver、namespace allowlist、service allowlist、extra matchers 的 canonical JSON 计算 SHA256 前 16 位。这个 hash 同时用于：

- Redis lock key；
- `AlertPollCursor.filter_hash`；
- poll audit `resource_id`；
- 同一 alert 在不同 poll scope 下的 cursor 隔离。

allowlist 的排序会 canonicalize，所以 `prod,staging` 和 `staging,prod` 产生同一个 hash。

## 7. Server-side Matcher 映射

轮询时优先把 scope 下推到 Alertmanager API：

```text
_allowlist_to_server_matchers(namespace_allowlist, service_allowlist, service_label)
-> namespace=~"prod|staging"
-> {service_label}=~"checkout|payment"
```

`service_label` 来自 effective config 的 `metrics_service_label`，默认语义是把本系统的 service 概念映射到实际指标/alert label。

`ALERT_POLL_FILTER_MATCHERS` 会追加到 `filter[]` 参数。`AlertmanagerClient.list_alerts()` 用：

- `filter` 参数传 Alertmanager matcher；
- `receiver` 参数传 receiver filter；
- `timeout` 使用 `ALERT_POLL_TIMEOUT_SECONDS`。

客户端只读 Alertmanager `/api/v2/alerts`，不会修改 Alertmanager 配置或 silence。

## 8. Poll Loop 的 Incident 创建

`_poll_alertmanager_logic()` 的核心流程：

```text
read latest published EffectiveConfigVersion
-> EffectiveConfig.from_operator_sources(...)
-> alertmanager URL missing: degraded
-> AlertmanagerClient.list_alerts()
-> cap raw alerts by ALERT_POLL_MAX_ALERTS_PER_ROUND
-> for each alert:
     parse _from_alertmanager_single_alert()
     skip if cursor already_seen_active(fingerprint, filter_hash)
     enforce per-service creation cap
     AlertService.create_alert(AlertCreateRequest(...))
     cursor.mark_seen(fingerprint, incident_id, filter_hash)
-> mark missing fingerprints
-> conservative resolved inference
-> audit completed
```

每轮有三层创建控制：

| 控制 | 配置/对象 | 作用 |
|------|-----------|------|
| 每轮扫描上限 | `ALERT_POLL_MAX_ALERTS_PER_ROUND` | 避免单轮处理过多 alert |
| 每轮新 incident 上限 | `ALERT_POLL_MAX_NEW_INCIDENTS_PER_ROUND` | 避免新 incident 激增 |
| 每服务每分钟新 incident 上限 | `ALERT_POLL_MAX_INCIDENTS_PER_SERVICE_PER_MINUTE` | 避免单服务噪音打爆队列 |

即使 poll cursor 漏过重复，`AlertService.create_alert()` 仍会按 open incident fingerprint 做最终 dedup。

## 9. Poll Cursor 事务边界

`AlertPollCursor` 字段：

| 字段 | 含义 |
|------|------|
| `filter_hash` | poll scope hash |
| `fingerprint` | 该 scope 中观察到的 alert fingerprint |
| `incident_id` | 创建或关联的 incident |
| `first_seen_at` | 首次观察时间 |
| `last_seen_at` | 最近观察时间 |
| `missing_rounds` | 连续缺失轮数 |

唯一约束是 `(filter_hash, fingerprint)`。

`PollCursorRepository` 是当前持久化层里一个有意的例外：方法内部会 `commit()`。这是为了在长 poll loop 中及时保存 `last_seen_at` 和 `missing_rounds`，让后续轮次和 resolved inference 能看到最新 cursor 状态。

主要方法：

| 方法 | 行为 |
|------|------|
| `already_seen_active()` | 如果 cursor 已存在，更新 `last_seen_at` 并把 `missing_rounds` 置 0，然后返回 true |
| `mark_seen()` | 创建或更新 cursor，写 incident_id、last_seen_at、missing_rounds=0 |
| `mark_missing()` | 对本轮未出现的 active fingerprint 递增 missing_rounds |
| `get_active_fingerprints()` | 返回某个 filter_hash 下 `missing_rounds == 0` 的 fingerprint |
| `get_filter_hashes_for_fingerprint()` | 返回曾经见过该 fingerprint 的所有 filter hash |

这个内部 commit 行为意味着：修改 poll cursor repository 时不能假设它和外层 worker 事务完全绑定。

## 10. Resolved Inference

Alertmanager poll 读取的是“当前 active alerts”。当某个 fingerprint 不再出现在当前 poll 结果里，系统不能立刻把 incident 标为 resolved，因为可能是：

- 本轮结果被截断；
- Alertmanager 暂时不可达；
- scope 配置变化；
- alert 在其他 filter hash 仍可见；
- 刚创建不久，还在宽限期内。

`infer_resolved_from_missing_fingerprints()` 的规则按顺序执行：

1. 如果本轮结果被 `ALERT_POLL_MAX_ALERTS_PER_ROUND` 截断，禁止 resolved inference。
2. 如果 fingerprint 还在 `ALERT_POLL_RESOLVED_GRACE_PERIOD_SECONDS` 派生出的 grace rounds 内，不推断 resolved。
3. 只考虑曾经见过该 fingerprint 的 filter hash。
4. 所有参与的 filter hash 都必须连续 missing 至少 `ALERT_POLL_RESOLVED_MISSING_ROUNDS`。
5. 满足后，只把 open incident 的 `status` 标为 `resolved`，并写 audit log。

当前 worker 调用中 `all_active_filter_hashes` 使用当前单轮 `filter_hash`，但函数本身会通过 cursor repository 查询该 fingerprint 曾被哪些 filter hash 看到过。这个设计保证多 scope 参与时不会因为一个 scope 缺失就过早 resolved。

生产上应记住：poll resolved 是本系统基于 active alert 缺失的保守推断，不等同于 Alertmanager webhook 的 resolved event。如果必须精确记录 resolved 时间，应优先使用 Alertmanager webhook 或 `ALERT_SOURCE=both`。

## 11. Audit 和 Metrics

通用告警入口的主要可观测对象：

- API response 的 `X-Request-Id`；
- incident 和 agent run；
- `celery_task_id`；
- rate limit 结果；
- NFA suppression 结果；
- notification enqueue 尝试。

Alertmanager poll 额外写 audit：

```text
actor=poll_alertmanager
action=alertmanager.poll
resource_type=alert_poll
resource_id={filter_hash}
details={filter_hash, status, error?, counts...}
source=beat
```

resolved inference 成功时写：

```text
action=incident.resolved_inferred
actor=poll_alertmanager
details={fingerprint, filter_hash, reason, evidence}
```

Grafana helper metrics：

| Metric | 触发 |
|--------|------|
| `agentp_grafana_webhook_ignored_total{reason="disabled"}` | helper 被调用但 `GRAFANA_ALERT_INGEST_ENABLED=false` |
| `agentp_grafana_webhook_ingest_total{status="malformed"}` | helper payload 不是 dict 或缺少 `alerts` list |
| `agentp_grafana_webhook_ingest_total{status="success"}` | helper 成功进入 `create_alert()` |

由于 helper 当前没有公开 router，这些 metrics 只有在代码路径显式调用 helper 时才会增长。

## 12. 配置清单

### 通用 alert source

| 配置 | 默认 | 当前含义 |
|------|------|----------|
| `ALERT_SOURCE` | `webhook` | `webhook` 只使用 `/api/alerts`；`poll` 只启用 Alertmanager poll；`both` 同时启用；`none` 会让 poll 跳过，但不关闭 `/api/alerts` router |

### Alertmanager poll

| 配置 | 默认 | 当前含义 |
|------|------|----------|
| `ALERTMANAGER_URL` | `http://localhost:9093` | settings 默认 URL；production worker 会优先从 published EffectiveConfig 合并读取 |
| `ALERTMANAGER_READ_TOKEN` | unset | 配置字段存在；`AlertmanagerClient` 支持 auth 参数，但当前 `poll_alertmanager` 构造 client 时没有传入该 token |
| `ALERT_POLL_INTERVAL_SECONDS` | `30` | Beat 调度间隔 |
| `ALERT_POLL_LOCK_TTL_SECONDS` | `60` | Redis poll lock TTL |
| `ALERT_POLL_TIMEOUT_SECONDS` | `20` | Alertmanager request timeout |
| `ALERT_POLL_RESOLVED_GRACE_PERIOD_SECONDS` | `120` | resolved 推断宽限时间 |
| `ALERT_POLL_RESOLVED_MISSING_ROUNDS` | `3` | 连续 missing 轮数阈值 |
| `ALERT_POLL_RECEIVER_FILTER` | empty | receiver scope |
| `ALERT_POLL_FILTER_MATCHERS` | empty | Alertmanager matcher 表达式 |
| `ALERT_POLL_NAMESPACE_ALLOWLIST` | empty | namespace allowlist |
| `ALERT_POLL_SERVICE_ALLOWLIST` | empty | service allowlist |
| `ALERT_POLL_MAX_ALERTS_PER_ROUND` | `200` | 每轮扫描上限 |
| `ALERT_POLL_MAX_NEW_INCIDENTS_PER_ROUND` | `20` | 每轮新 incident 上限 |
| `ALERT_POLL_MAX_INCIDENTS_PER_SERVICE_PER_MINUTE` | `5` | 单服务每分钟新 incident 上限 |

### Grafana

| 配置 | 默认 | 当前含义 |
|------|------|----------|
| `GRAFANA_ALERT_INGEST_ENABLED` | `false` | 控制 `AlertService.ingest_grafana_alert()` helper；当前 helper 直接读取该 setting，未叠加 M9 resolver，且没有公开独立 router |
| `GRAFANA_WEBHOOK_SECRET_REF` | empty | 配置字段存在；当前未接入已注册 route 的 HMAC 校验 |
| `GRAFANA_WEBHOOK_MAX_BYTES` | `256000` | 配置字段存在；当前未接入已注册 route 的 payload size 校验 |

## 13. 常见排查

### `/api/alerts` 返回 deduplicated

检查：

- 请求 fingerprint；
- 是否存在同 fingerprint 的 open incident；
- incident latest run 状态；
- 是否期望手动重新诊断，如果是，用 `POST /api/incidents/{incident_id}/diagnose`。

### 告警已进入 API 但没有 run

检查：

- `AlertService.create_alert()` 是否在创建 incident/run 后 Celery 入队失败；
- incident/run 是否被标记为 failed；
- Redis broker 是否 ready；
- response `celery_task_id` 是否为空。

### Poll 没有创建 incident

检查：

- `ALERT_SOURCE` 是否为 `poll` 或 `both`；
- poll scope 是否有效，不要只配置 severity/priority；
- Redis lock 是否已有同 filter hash 任务持有；
- EffectiveConfig 或 settings 中 Alertmanager URL 是否为空；
- Alertmanager API 是否返回 active alerts；
- 是否触发 `already_seen_active()` 或 open fingerprint dedup；
- 是否达到每轮或每服务新 incident cap。

### Poll 误以为 resolved 或迟迟不 resolved

检查：

- 本轮是否被 truncation 阻断 resolved inference；
- `first_seen_at` 是否仍在 grace period；
- `missing_rounds` 是否达到阈值；
- 同 fingerprint 是否被其他 filter hash 看到过；
- incident 是否仍是 open 状态。

### Grafana HMAC 没生效

当前没有公开独立 Grafana webhook router，也没有 route 使用 `GRAFANA_WEBHOOK_SECRET_REF` 做 HMAC。若请求走的是 `/api/alerts`，它受通用 API key、rate limit 和 schema validation 保护，不会读取 Grafana webhook secret 配置。

## 14. 测试覆盖索引

| 行为 | 测试 |
|------|------|
| Alert API 创建/去重 | `tests/integration/test_alert_api.py` |
| Alertmanager poll parser/filter/cursor | `tests/integration/test_poll_integration.py`、`tests/unit/test_poll_cursor.py` |
| Matcher parser/scope | `tests/unit/test_matcher_parser.py` |
| Resolved inference | `tests/unit/test_resolved_inference.py` |
| Alertmanager client | `tests/unit/test_alertmanager_client.py` |
| Grafana parser/fingerprint | `tests/unit/test_grafana_alert_parser.py` |
| Grafana-shaped `/api/alerts` E2E | `tests/e2e/test_m9_tempo_grafana.py` |
| Settings defaults | `tests/unit/test_settings_production_defaults.py` |

按项目规则，Codex 不直接运行 pytest、frontend tests 或 Playwright。修改这些路径后，建议由用户本地运行对应测试文件或完整门禁。

## 15. 修改这些能力时的文档同步

| 改动 | 同步文档 |
|------|----------|
| 新增 alert source 或 parser | 本文、[API 参考](../01-backend/api-reference.md)、[配置参考](../11-reference/configuration.md) |
| 修改 `/api/alerts` 事务/入队行为 | 本文、[告警到报告技术深挖](alert-to-report-deep-dive.md)、[API 控制面与服务层技术深挖](api-control-plane-service-deep-dive.md) |
| 修改 Alertmanager poll scope/cursor/resolved | 本文、[Celery 与任务](../01-backend/celery-and-jobs.md)、[状态与 ID](../11-reference/status-and-ids.md)、[生产发布、运维与回滚技术深挖](production-operations-rollback-deep-dive.md) |
| 暴露独立 Grafana webhook route | 本文、[M9 发布计划](../m9-rollout.md)、[M9 数据流](../m9-data-flow.md)、[M9 威胁模型](../m9-threat-model.md)、[配置参考](../11-reference/configuration.md)、release checklist |
| 将 `GRAFANA_WEBHOOK_SECRET_REF` / `GRAFANA_WEBHOOK_MAX_BYTES` 接入 route | 本文、[M9 威胁模型](../m9-threat-model.md)、[最终执行前清单](../final-pre-execution-checklist.md)、[运维 Runbook](../10-operations/runbook.md) |
