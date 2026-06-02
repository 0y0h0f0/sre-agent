# Phase 2：工具层实战化（数据打通）

目标：把工具层从 Mock/fixture 推到真实后端，从 Mock 到真实。对应工具实现见 `03-tools/tools.md`，风险分级见 `02-agent/guardrails-and-approval.md`。

> **实现状态（已落地，2.1-2.4）**：工具层改为「可插拔后端 + factory」结构，所有后端默认 `fixture`，保持测试确定性与本地离线；切到真实后端只改配置，不改调用方。
> - 2.1：Trace（`packages/tools/trace_backends.py`：fixture | jaeger | tempo）、Deployment（`packages/tools/deployment_backends.py`：fixture | github | argocd）后端化；PromQL/LogQL 的 service label 可配置；大时间窗口自动分片**且不截断**——超过 `metrics_max_shards` 时加宽分片窗口并按比例放粗 step 以覆盖整窗（避免静默丢尾部数据）；缓存 key 增加 datasource 维度。
> - 2.2：`packages/tools/k8s.py` 提供只读诊断（describe/logs/events/rollout status）；非只读操作在工具层被直接拒绝；写类动作仅由 `build_remediation_suggestions` 产出 `--dry-run` 建议命令（`executed=False`、需审批），从不执行。
> - 2.3：`packages/tools/db_diagnostics.py` 只读诊断（pg_stat_activity / pg_locks / pg_stat_statements）；live 后端用只读连接（`conn.read_only`）+ `statement_timeout` + `connect_timeout`，且 `_assert_read_only` 逐句拒绝非 SELECT。
> - 2.4：故障类型从 4 种扩展到 15 种（`packages/agent/fake_llm.py`），新增对应 PromQL 模板（`cpu_throttle`/`disk_avail`/`cert_expiry_days`/`dns_error_rate`/`queue_lag`/`rate_limit_hits`/`slo_burn_rate`），并在 `collect_metrics._metric_for_alert` 接通告警名 → 指标类型映射。
> - 图接入：新增 `collect_k8s` / `collect_db` 节点（`collect_deployment` → `collect_k8s` → `collect_db` → `retrieve_memory`），证据经 `build_context` 汇入诊断；两节点在工具缺省（如 eval harness，`deps.k8s_tool`/`db_diagnostics_tool` 为 None）时安全空转，保持既有测试确定性。
>
> **边界处理（按优先级）**：
> 1. **相关性门控**：`collect_k8s`/`collect_db` 仅在故障类别确实涉及该层时采集（关键字 + P0 兜底），避免把无关 pod/db 状态注入证据与交叉验证。
> 2. **交叉验证接入**：K8s/DB 作为新的印证源进入 `evidence_validation`（权重 Trace>Metrics>**K8s**>Logs>**DB**>Deploy；OOM/CrashLoop/BackOff、连接池饱和/idle-in-txn/重锁/慢查询触发 anomaly）；仅在相关时采集，故只印证不引入噪声。
> 3. **指标分片不截断**：见上。
> 4. **Deployment 历史**：GitHub API 无时间过滤，改为 `per_page=100` 客户端按窗口过滤（深翻页为后续项）。
> 5. **Live 覆盖**：LiveDbBackend 只读路径（无 `SET TRANSACTION READ ONLY`、connect/statement timeout）与 LiveK8sBackend events 路径已用 mock 单测覆盖。
>
> **边界遵从**：K8s/DB 均为只读默认 fixture；无真实生产写操作，L4 仍直接拒绝，L3 仍二次确认。放宽 scope 的真实写操作未实现，需单独立项。待真实环境验证项：Jaeger/Tempo/GitHub/Argo CD/K8s/PG 的端到端 live smoke。

## 2.1 Prometheus / Loki 查询增强与 Fixture 替换

目标：Prometheus 和 Loki 当前已经通过 HTTP 查询真实后端；此阶段重点是增强查询模板、生产安全性，并把仍使用 fixture 的 Trace / Git deployment 数据源替换为真实后端。

| 任务 | 细节 |
| --- | --- |
| PromQL 模板 | 预置常用查询：`rate(http_requests_total{status=~"5.."}[5m])`、`up`、`cpu_usage`；保留 service label 映射配置 |
| LogQL 模板 | 按 service + time range + keyword 构建 LogQL，明确 label key 可配置，不假设所有环境都叫 `service` |
| 查询优化 | 大时间窗口自动分片查询，限制 step / limit / timeout，避免 Prometheus/Loki OOM |
| 缓存策略 | 同一时间窗口 + 同一查询的缓存复用，缓存 key 包含 datasource、tenant、service、time bucket |
| Trace 后端 | 将 `TraceTool` 从 `demo/faults/traces.json` 替换为 Jaeger/Tempo/OTel 查询适配器 |
| Deployment 后端 | 将 `GitChangeTool` 从 `demo/faults/git_changes.json` 替换为 GitHub/GitLab/Argo CD/Flux 查询适配器 |

## 2.2 Kubernetes 集成

目标：优先做 K8s 只读诊断；写操作只允许 staging 或 dry-run 试点，不对真实生产集群执行。

| Level | 动作 | 说明 |
| --- | --- | --- |
| L0 | `kubectl describe pod`、`kubectl logs`、`kubectl get events`、`kubectl rollout status` | 只读诊断，可自动执行 |
| L1 | 创建工单、生成建议命令、生成 `kubectl diff` / dry-run 结果 | 不修改集群状态 |
| L2 | `kubectl cordon`、`kubectl rollout restart`、`kubectl scale` | 仅 staging 或显式 dry-run；需审批 |
| L3 | `kubectl rollout undo` | 仅 staging 或显式 dry-run；需审批 + 二次确认 |

**硬边界**：`00-overview/scope.md` 明确不做真实生产 K8s 写操作。生产集群 service account 默认只读；任何写动作必须通过环境白名单、dry-run 标志、审批记录、审计日志和 guardrail 校验。

## 2.3 数据库直连诊断

目标：连接数据库获取实时状态。

| 任务 | 细节 |
| --- | --- |
| 连接池诊断 | `pg_stat_activity`、`pg_locks`、连接数、等待事件 |
| 慢查询分析 | `pg_stat_statements` Top N |
| 只读保证 | 所有 DB 操作限制为只读查询，使用只读账号、`statement_timeout`、`SET TRANSACTION READ ONLY`，封装在 dedicated tool 中 |

## 2.4 更多故障类型

目标：从 4 种 MVP 故障扩展到 15+ 种。

| 新增类型 | 诊断要点 |
| --- | --- |
| CPU 节流 | `container_cpu_throttled` → request/limit 不合理 |
| 内存泄漏 | OOMKilled → `memory_working_set_bytes` 持续增长 |
| 磁盘满 | `node_filesystem_avail` → 日志轮转 / PVC 扩容 |
| 证书过期 | `tls_cert_expiry` → 提前 N 天告警 |
| DNS 故障 | CoreDNS 丢包 → 解析超时 |
| 消息积压 | Kafka/Redis Stream consumer lag |
| 限流触发 | `rate_limit_hits` → 检查限流策略是否合理 |
| 慢 API | P95 延迟飙升 → Trace 分析瓶颈 span |
| 错误预算耗尽 | SLO burn rate 过高 |
| P0 级全站宕机 | 多服务同时告警 → 自动提级 + 加急通知 |
