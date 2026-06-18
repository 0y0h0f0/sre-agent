# 生产发布、运维与回滚技术深挖

**最后更新：** 2026-06-18

本文沿当前代码和部署路径说明生产发布门禁、Day-2 运维、M9 受控增强和回滚如何协作。它补充 [生产环境检查清单](../production-checklist.md)、[最终执行前发布门禁](../final-pre-execution-checklist.md)、[运维 Runbook](../10-operations/runbook.md)、[Day-2 运维操作手册](../operator-runbook.md) 和 [M9 Rollout](../m9-rollout.md)：这些文档列出清单和命令；本文解释每个门禁背后的实现入口、配置来源、运行时信号和失败处理。live executor 的 action capability、snapshot/preflight/verify/replan 细节见 [执行器、动作能力与验证闭环技术深挖](executor-action-verification-loop-deep-dive.md)。

## 阅读目标

读完本文应能回答：

- 代码默认值、Docker Compose 默认值、K8s base manifest 和 production overlay 有什么差异。
- 为什么 `APP_ENV=production` 只改变未显式设置的 `LLM_PROVIDER` 和 `DISCOVERY_ENABLED`。
- `/healthz`、`/readyz`、`/metrics` 各自证明什么，不能证明什么。
- API/worker/beat 在 Compose 和 K8s 中如何启动，迁移由谁执行，Beat 为什么必须单实例。
- 发布前 P0/P1/M9 门禁分别对应哪些代码边界和测试入口。
- live diagnostics、live executor、Alertmanager poll、discovery、M9 外部调用在生产里如何受控启用。
- 回滚时应该先关闭哪些开关，如何验证回滚已经生效。
- 事故排查时先看哪些表、日志、指标和配置。

## 代码与文档入口

| 主题 | 当前入口 |
|------|----------|
| Runtime settings | `packages/common/settings.py` |
| M9 feature flag resolver | `packages/common/feature_flags.py` |
| Backend URL safety | `packages/common/backend_url_safety.py` |
| Redaction | `packages/common/redaction.py` |
| Prometheus metrics | `packages/common/metrics.py` |
| Health/readiness/metrics router | `apps/api/routers/health.py` |
| API auth middleware | `apps/api/middleware/auth.py` |
| Config override API | `apps/api/routers/config.py` |
| Audit repository | `packages/db/repositories/audit_logs.py` |
| Worker dependency builder | `apps/worker/tasks.py` |
| Celery app/Beat | `apps/worker/celery_app.py` |
| Live executor | `packages/tools/executor_backends.py` |
| Guardrail policy | `packages/agent/guardrails/policy.py` |
| Compose runtime | `docker-compose.yml` |
| Production image | `Dockerfile.prod` |
| K8s base manifests | `deploy/k8s/base/` |
| K8s production overlay | `deploy/k8s/overlays/production/` |
| Release checklist | `docs/production-checklist.md`、`docs/final-pre-execution-checklist.md` |
| Operations runbooks | `docs/10-operations/runbook.md`、`docs/operator-runbook.md` |
| M9 rollout/threat model | `docs/m9-rollout.md`、`docs/m9-threat-model.md` |

## 总链路

```text
release candidate
  -> freeze git commit / image tag / migration version
  -> run deterministic gates
       -> ruff / mypy / pytest unit+integration coverage
       -> FakeLLM smoke eval
       -> Vitest coverage / build / Playwright smoke
  -> validate production config
       -> APP_ENV=production
       -> auth enabled
       -> fixture executor unless explicit live drill
       -> M9 default-off baseline
       -> backend URL safety and secret redaction
  -> deploy
       -> Compose or K8s base/overlay
       -> API runs migrations before serving
       -> worker/beat use already-migrated schema
  -> verify runtime
       -> /healthz
       -> /readyz
       -> /metrics
       -> worker logs / Celery queue
       -> smoke eval or manual smoke path
  -> observe first 24h
       -> agentp_* metrics
       -> audit logs
       -> run/node/tool/action/approval records
  -> rollback if needed
       -> disable live/external/M9/poll/discovery
       -> restore trace settings
       -> restart API/worker/beat where config changed
       -> re-check /readyz, approval flow, smoke eval, metrics
```

Codex 在本项目中不直接运行 `pytest`、前端测试、Playwright 或完整测试套件。本文保留命令用于开发者、CI 和运维人员复现。

## 1. Runtime Profiles

生产门禁首先要区分四种配置来源。

### Code Defaults

`Settings` 的普通默认值面向本地 demo：

| 字段 | 普通默认 |
|------|----------|
| `APP_ENV` | `local` |
| `LLM_PROVIDER` | `fake` |
| `EXECUTOR_BACKEND` | `fixture` |
| `TRACE_BACKEND` | `fixture` |
| `TRACE_ENABLED` | `true` |
| `DISCOVERY_ENABLED` | `true` |
| `ALERT_SOURCE` | `webhook` |
| `M9_EXTENSIONS_ENABLED` | `false` |
| `API_KEY_AUTH_ENABLED` | `true` in `Settings` class |

`Settings._apply_production_safety_defaults()` 只在 `APP_ENV=production` 且字段未显式设置时修改：

```text
if APP_ENV == production and LLM_PROVIDER not explicit:
  LLM_PROVIDER = disabled
if APP_ENV == production and DISCOVERY_ENABLED not explicit:
  DISCOVERY_ENABLED = false
```

它不会自动改：

- `EXECUTOR_BACKEND`，因为默认已是 `fixture`。
- `API_KEY_AUTH_ENABLED`，因为生产必须显式确认部署层值。
- `M9_EXTENSIONS_ENABLED`，因为默认已是 `false`。
- `TRACE_BACKEND`，因为 Jaeger/Tempo/disabled/fixture 需要按环境选择。

因此文档和发布记录必须写清“代码生产默认”和“部署显式覆盖”。

### Docker Compose

`docker-compose.yml` 是本地完整 demo：

- 默认启动 13 个服务：postgres、redis、prometheus、loki、promtail、otel-collector、bge-zh、grafana、demo-service、web、api、worker、beat。
- `mailpit` 在 `dev` profile 中启动。
- API command 是 `alembic upgrade head && uvicorn ... --reload`。
- Worker command 是 `celery -A apps.worker.tasks:celery_app worker --loglevel=INFO`。
- Beat command 是 `celery -A apps.worker.tasks:celery_app beat --loglevel=INFO`。
- Compose API/worker/beat 默认 `LLM_PROVIDER=fake`、`EXECUTOR_BACKEND=fixture`、`DISCOVERY_ENABLED=true`、`API_KEY_AUTH_ENABLED=false`，这是本地 demo 口径，不是生产口径。

### K8s Base

`deploy/k8s/base/configmap.yaml` 是安全的集群基础清单，但仍偏 base/demo：

- `APP_ENV=local`。
- `LLM_PROVIDER=fake`。
- `EXECUTOR_BACKEND=fixture`。
- `TRACE_BACKEND=fixture`。
- `K8S_BACKEND=fixture`。
- `DISCOVERY_ENABLED=true`。
- `M9_EXTENSIONS_ENABLED=false`。
- `API_KEY_AUTH_ENABLED=true`。
- `BACKEND_URL_ALLOWLIST=*.svc.cluster.local,*.svc,kubernetes.default.svc`。

Base RBAC 是只读诊断权限；live executor 写权限不在 base 中。

### K8s Production Overlay

`deploy/k8s/overlays/production/configmap-patch.yaml` 显式覆盖：

- `APP_ENV=production`。
- `LLM_PROVIDER=disabled`。
- `EXECUTOR_BACKEND=fixture`。
- `TRACE_BACKEND=disabled`。
- `TRACE_ENABLED=false`。
- `K8S_BACKEND=live`，用于只读 K8s diagnostics。
- `K8S_NAMESPACE=production,staging`。
- `DISCOVERY_ENABLED=true`，这是显式覆盖代码的 production default。
- `API_KEY_AUTH_ENABLED=true`。

`replica-patch.yaml` 把 API 和 worker 扩到 3 副本；Beat 仍在 base 中保持 1 副本。

## 2. Health, Readiness, Metrics

`apps/api/routers/health.py` 暴露：

| Endpoint | 实现 | 说明 |
|----------|------|------|
| `GET /healthz` | 返回 `{"status":"ok"}` | 只证明 API 进程存活。 |
| `GET /readyz` | `SELECT 1` + Redis ping + Celery broker Redis ping | 证明 Postgres、Redis、Celery broker 可用。 |
| `GET /metrics` | `prometheus_client.generate_latest()` | 暴露进程内 Prometheus metrics。 |

`/readyz` 不检查：

- Prometheus/Loki/Trace/K8s/DB diagnostics 等业务后端。
- Worker 是否正在消费任务。
- Discovery proposal 是否已发布。
- M9 子功能是否可用。

生产上线、回滚和扩缩容后最小检查：

```bash
curl http://<api>/healthz
curl http://<api>/readyz
curl http://<api>/metrics
```

如果要确认业务后端真的接入，应再看一次真实 Agent run 的 `tool_calls`，详见 [K8s 后端对接验证](../08-deploy/k8s-backend-verification.md)。

## 3. Process Topology

| 进程 | Compose | K8s base | 生产注意事项 |
|------|---------|----------|--------------|
| API | `alembic upgrade head && uvicorn --reload` | `alembic upgrade head && uvicorn`，2 副本 | 多副本发布前要确认 migration 策略；高风险 migration 不应靠多 Pod 并发碰运气。 |
| Worker | Celery worker | Celery worker `--concurrency=4`，2 副本 | 可水平扩容；依赖 row lock、idempotency 和 checkpoint。 |
| Beat | Celery beat | 1 副本 | 必须单实例；多 Beat 会重复触发 summary、stale approval、poll、discovery。 |
| Web | Vite dev server in Compose | nginx static web | 通过 API client 调后端，不决定风险等级。 |

K8s worker liveness probe 使用：

```text
PROMETHEUS_METRICS_ENABLED=false exec celery -A apps.worker.tasks:celery_app inspect ping -d celery@$HOSTNAME
```

这避免 probe 进程启动独立 worker metrics server。

## 4. Release Gate Mapping

P0 发布门禁对应的实现入口：

| 门禁 | 实现或验证入口 |
|------|----------------|
| `APP_ENV=production` | `Settings.app_env`、部署 ConfigMap/Secret。 |
| LLM 稳定路径 | `Settings._apply_production_safety_defaults()`、`packages/agent/llm/factory.py`。 |
| fixture executor 默认 | `build_executor_backend()` 默认返回 `FixtureExecutorBackend`。 |
| API auth | `apps/api/middleware/auth.py`、`API_KEY_OPEN_PATHS`。 |
| backend URL safety | `BackendUrlSafetyValidator`，生产阻断 localhost、metadata、link-local/private IP，allowlist 例外。 |
| raw secret 不外泄 | `SecretStr`、`redaction.py`、`backend_auth.py`、M9 prompt/web/embedding redaction。 |
| L2/L3/L4 | `guardrails/policy.py`、approval service、`human_approval` node。 |
| checkpoint fail closed | `apps/worker/tasks.py:_build_checkpointer()`。 |
| Redis/Celery readiness | `/readyz`。 |
| Beat 单例 | `deploy/k8s/base/beat.yaml` replicas=1。 |
| CI/FakeLLM smoke | `.github/workflows/ci.yml`、`packages/evals/datasets/harness.py`。 |
| Alertmanager poll scope | `poll_alertmanager` task、poll scope validation tests。 |
| M9 global default-off | `resolve_m9_feature_flags()`。 |

发布记录必须把这些转成证据，而不是只写“已检查”。

## 5. Database and Migration Gate

当前 API 启动命令会执行 `alembic upgrade head`。

发布前必须明确：

- 当前 DB migration version。
- 发布镜像 tag/commit。
- migration 是否只增量兼容。
- 高风险 migration 是否有 downgrade 演练或不可逆说明。
- 多 API 副本启动时是否可能并发执行 migration。

建议发布记录包含：

```text
git_commit:
image_tag:
alembic_before:
alembic_after:
migration_risk:
rollback_db_plan:
```

不要用 schema downgrade 作为 M9 或 live backend 的常规回滚路径。优先使用 feature flag、executor/config rollback 和 trace rollback。

## 6. Auth, Scope, and Open Paths

`Settings.api_key_open_paths` 默认：

```text
/healthz,/readyz,/metrics,/docs,/openapi.json,/api/approvals/by-token
```

生产要求：

- `API_KEY_AUTH_ENABLED=true`。
- `API_KEY_INITIAL_SEED` 只用于 bootstrap，创建真实 key 后移除或轮换。
- `api_key:admin` 不应被当成业务写 scope。
- Config/discovery/M9 review endpoints 仍要用 `require_scope()`。
- WebSocket 不能携带普通 Authorization header 时，使用 ticket 机制，不把 bearer token 放到长期 URL。

Email token 路径只用于受控审批链接；L3 不允许通过 email token 批准。

## 7. Backend URL Safety and Config Override

`BackendUrlSafetyValidator` 的生产规则：

- 只允许 `http` / `https`。
- URL username/password 被拒绝。
- metadata endpoint 永远拒绝。
- production 拒绝 localhost、loopback、link-local、private IP，除非明确 allowlist。
- 可配置 allowed/blocked domains、HTTPS requirement、DNS resolve 校验。
- 可用 K8s evidence 允许已发现的 cluster service。

通用 override API 禁止字段：

```text
secret, secrets, auth, auth_config,
executor_backend, executor, live, bearer_token,
password, private_key, client_cert, client_key
```

这意味着生产紧急 override 可以调整受控 backend URL/label/mapping，但不能通过通用 override 打开 live executor、写 secret 或绕过 auth。

## 8. Discovery and Alertmanager Poll

Discovery：

- 生产代码默认 `DISCOVERY_ENABLED=false`，除非部署显式覆盖。
- K8s production overlay 显式 `DISCOVERY_ENABLED=true`。
- Worker 只读取 latest published `EffectiveConfigVersion`，不读取 pending proposal。
- Production discovery 不应 auto-publish unsafe backend；proposal 至少需要 review。
- `auto_discovery_rerun` 需要 discovery enabled 且 K8s backend live，并使用 Redis lock。

Alertmanager poll：

- `ALERT_SOURCE=webhook` 默认只接收 webhook。
- `poll` 或 `both` 才启用 poll task。
- 必须有 receiver、matcher、namespace allowlist、service allowlist 或 extra matcher 形成有效 scope。
- 使用 Redis lock `lock:poll:alertmanager:{filter_hash}` 防并发。
- 通过 `AlertPollCursor` 和 missing rounds 做 conservative resolved inference。

发布前不要启用无边界 poll scope。

## 9. Live Diagnostics and Executor

Live diagnostics 与 live executor 是不同风险面。

| 能力 | 配置 | 风险边界 |
|------|------|----------|
| Live K8s diagnostics | `K8S_BACKEND=live` | 只读 describe/logs/events/rollout/get deployment/get statefulset。 |
| Live DB diagnostics | `DB_DIAGNOSTICS_BACKEND=live` | 只读账号，预定义 SELECT，read-only transaction，statement timeout。 |
| Live executor | `EXECUTOR_BACKEND=live` | 真实 K8s mutation，必须显式 opt-in、guardrail、approval、snapshot、verify。 |

`LiveK8sExecutorBackend` 当前 handler 限定在现有支持集合内，包括 Deployment/StatefulSet rolling restart、rollout pause/resume、Deployment scale、Deployment rollback，以及部分兼容 action name。不要把它扩展成任意 Kubernetes patch。

Live executor 发布前额外检查：

- `EXECUTOR_BACKEND=live` 是否有单独审批。
- `EXECUTOR_K8S_NAMESPACE` 是否限定目标 namespace。
- ServiceAccount 是否只有目标 namespace 的最小写权限。
- L2/L3 审批链路是否演练过。
- L3 `risk_ack`、`confirm_action_type`、`confirm_target` 是否被后端验证。
- L4 destructive action 是否直接拒绝且不进入 approval。

## 10. M9 Rollout and Rollback

M9 的核心规则：

- `M9_EXTENSIONS_ENABLED=false` 强制所有 M9 子功能 disabled。
- 子功能 true + 全局 false 会记录 conflict log 和 `agentp_m9_feature_flag_conflict_total`，但不阻止服务启动。
- Jaeger 是 M8 能力；M9 false 不禁用 Jaeger。
- Tempo 是 M9 trace backend；M9 false + `TRACE_BACKEND=tempo` 会 degraded/conflict。
- 外部 LLM 需要相关 M9 子功能和 `LLM_EXTERNAL_PROVIDER_ALLOWED=true`。
- Web search、external embedding、LLM prompt 都必须 redaction、timeout、audit/metric、degraded path。
- LLM 只能生成 pending review draft/amendment，不能发布、审批或执行。

启用 Tempo 或其它 M9 trace 相关能力前记录：

```bash
export PRE_M9_TRACE_BACKEND=<current TRACE_BACKEND>
export PRE_M9_TRACE_ENABLED=<current TRACE_ENABLED>
```

单项回滚优先关闭子功能：

```bash
export RUNBOOK_LLM_GENERATION_ENABLED=false
export LLM_INCIDENT_DIFF_ENABLED=false
export RUNBOOK_WEB_SEARCH_ENABLED=false
export TEMPO_DISCOVERY_ENABLED=false
export GRAFANA_ALERT_INGEST_ENABLED=false
export SEMANTIC_RUNBOOK_SEARCH_ENABLED=false
export EXTERNAL_EMBEDDING_PROVIDER_ENABLED=false
export LLM_EXTERNAL_PROVIDER_ALLOWED=false
```

完全回滚：

```bash
export M9_EXTENSIONS_ENABLED=false
export TRACE_BACKEND=${PRE_M9_TRACE_BACKEND}
export TRACE_ENABLED=${PRE_M9_TRACE_ENABLED}
```

修改环境变量后，需要按部署方式重启 API、worker 和 beat。

## 11. Observability and Audit

关键 runtime 指标：

| 指标 | 用途 |
|------|------|
| `agentp_diagnosis_total` | diagnosis 成功/失败趋势。 |
| `agentp_diagnosis_duration_seconds` | diagnosis 耗时分布。 |
| `agentp_active_diagnoses` | in-flight run 是否堆积。 |
| `agentp_tool_call_total` | 工具调用状态。 |
| `agentp_tool_cache_hit_total` / `agentp_tool_cache_miss_total` | 工具缓存效果。 |
| `agentp_approval_total` | 审批决策计数。 |
| `agentp_approval_response_time_seconds` | 审批响应时间。 |
| `agentp_llm_call_errors_total` | LLM provider 错误。 |
| `agentp_email_send_total` | 邮件发送结果。 |
| `agentp_m9_feature_enabled` | M9 feature resolved 状态。 |
| `agentp_m9_feature_flag_conflict_total` | M9 全局 gate 冲突。 |
| `agentp_m9_secret_redaction_failures_total` | M9 secret redaction failure。 |

Worker metrics server：

- 由 `PROMETHEUS_METRICS_ENABLED` 和 `CELERY_METRICS_PORT` 控制。
- 只在真实 worker 进程且非 eager 模式启动。
- Compose 未映射 worker metrics 端口；生产采集要在部署层暴露或用 ServiceMonitor/Service 抓取。

Audit：

- `AuditLogRepository` 只提供 create/query，不提供 update/delete。
- 注释中明确生产应增加 DB trigger 阻止 raw UPDATE/DELETE。
- Config、discovery、approval、comment、feedback、runbook review 等写路径应记录 audit。
- Alertmanager poll 仍有 legacy `aud_` 前缀写入路径；不要把 public ID 前缀混用当成安全含义。

## 12. Rollback Playbooks

### 基础安全回滚

```bash
export EXECUTOR_BACKEND=fixture
export LLM_PROVIDER=disabled
export DISCOVERY_ENABLED=false
export ALERT_SOURCE=webhook
```

适用：

- live executor 风险。
- 真实 LLM provider 出错。
- discovery/poll 误配置。
- 需要恢复最小 webhook + deterministic worker 路径。

### Trace/M9 回滚

```bash
export M9_EXTENSIONS_ENABLED=false
export TRACE_BACKEND=${PRE_M9_TRACE_BACKEND}
export TRACE_ENABLED=${PRE_M9_TRACE_ENABLED}
```

适用：

- Tempo rollout 出错。
- M9 子功能冲突。
- 外部调用、redaction、allowlist、semantic/external embedding 发现异常。

### K8s Deployment 回滚

```bash
kubectl rollout undo deployment/api -n sre-agent
kubectl rollout undo deployment/worker -n sre-agent
```

注意：

- 这只回滚 Pod template，不自动回滚数据库 schema。
- 如果 ConfigMap/Secret 变更导致故障，必须回滚对应配置并重启相关 Deployment。
- Beat 如涉及调度配置也要重启。

回滚后最小复验：

```text
/readyz ready
worker no repeated errors
smoke eval or manual smoke path OK
L2/L3 approval flow OK
report generation OK
agentp_m9_feature_enabled expected 0
agentp_m9_feature_flag_conflict_total no new conflicts
```

## 13. Incident Debugging Matrix

| 现象 | 首看 | 判断方向 |
|------|------|----------|
| `/healthz` OK 但 `/readyz` 503 | `/readyz.dependencies`、Postgres/Redis/Celery broker | 基础依赖不可用，不是 Agent 逻辑问题。 |
| `/readyz` OK 但无业务 evidence | `tool_calls`、worker logs、EffectiveConfig | Prometheus/Loki/Trace/K8s/DB 后端不是 readiness 检查项。 |
| Worker 不消费 | Redis broker、worker logs、`agent_runs.status` | broker URL 是否一致、worker 是否在线、run 是否 orphan/waiting。 |
| Run 卡 waiting approval | `approvals`、`actions`、approval API responses | L3 字段、多审批 sibling、resume task 是否入队。 |
| M9 子功能未生效 | resolved feature flags、`agentp_m9_feature_flag_conflict_total` | 全局 gate false 或子功能 gate false。 |
| Tempo 配了但 degraded | `M9_EXTENSIONS_ENABLED`、`TRACE_BACKEND`、feature flag conflicts | Tempo 是 M9；Jaeger 不是 M9。 |
| Discovery 找到 endpoint 但 worker 没用 | `effective_config_versions.status` | Worker 只读 published，不读 pending proposal。 |
| Alertmanager poll 无 incident | poll scope、Redis lock、cursor、audit | scope 是否有效，filter hash 是否冲突，resolved inference 是否保守。 |
| Live executor 未执行 | action risk/status/approval、executor backend、RBAC | L2/L3 审批、L3 二次确认、namespace/name validation、K8s Role。 |
| 工程指标 hard fail | `/api/evals/engineering-metrics` 对应 metric | 优先排查 L2/L3 未审批执行、L3 confirmation、L4 未 block。 |

## 14. Release Record Template

建议每次生产发布或受控 live/M9 演练保留：

```text
release_id:
git_commit:
image_tag:
alembic_before:
alembic_after:
app_env:
llm_provider:
executor_backend:
trace_backend:
trace_enabled:
m9_extensions_enabled:
alert_source:
discovery_enabled:
api_key_auth_enabled:
backend_url_allowlist:
ci_backend_result:
ci_frontend_result:
smoke_eval_report:
manual_extra_tests:
enabled_live_or_m9_capabilities:
approver:
rollback_owner:
rollback_commands:
post_deploy_readyz:
post_deploy_smoke:
first_24h_observations:
```

不要把真实 secret、raw API key、SMTP password、LLM key、bearer token 或 kubeconfig 内容写进发布记录。

## 15. Verification Commands

开发者或运维人员可按风险选择执行：

```bash
ruff check apps packages tests
mypy apps packages
pytest tests/unit tests/integration --cov=apps --cov=packages --cov-report=term-missing --cov-fail-under=80
python -m packages.evals.runner --suite smoke --output reports/eval-smoke.json
```

```bash
cd apps/web
npm run test:coverage
npm run build
npm run test:e2e
```

```bash
APP_ENV=production python -c "from packages.common.settings import Settings; s=Settings(_env_file=None); assert s.llm_provider == 'disabled'; assert s.discovery_enabled is False; assert s.executor_backend == 'fixture'"
```

```bash
kubectl apply -k deploy/k8s/overlays/production/ --dry-run=client
kubectl -n sre-agent get cm sre-agent-config -o yaml
kubectl -n sre-agent rollout status deployment/api
kubectl -n sre-agent rollout status deployment/worker
```

Codex 不直接运行这些测试或发布命令；需要用户或 CI 执行并回贴结果。
