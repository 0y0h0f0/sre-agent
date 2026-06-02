# Phase 2：工具层实战化（数据打通）

目标：把工具层从 Mock/fixture 推到真实后端，从 Mock 到真实。对应工具实现见 `03-tools/tools.md`，风险分级见 `02-agent/guardrails-and-approval.md`。

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
