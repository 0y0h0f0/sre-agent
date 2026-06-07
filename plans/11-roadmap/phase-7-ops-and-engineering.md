# Phase 7：运维与工程化（生产化）

目标：从 Demo 到生产级。补齐认证授权、Agent 自身可观测性、A/B 评估和高可用。

> 完成记录：API key、Prometheus metrics、worker health、eval/shadow eval 和运维支撑已纳入当前实现；完整企业 RBAC/SSO 与高可用部署仍需按生产环境单独规划。

## 7.1 认证与权限

目标：从单机 demo 到团队使用。

| 任务 | 细节 |
| --- | --- |
| RBAC | Admin / SRE / Viewer 三级角色 |
| SSO | OIDC / OAuth2 对接（Keycloak, Okta, Google Workspace） |
| API Token | 服务间认证（worker → api），短期 token + 自动刷新 |

> MVP 不做 RBAC/SSO（见 `00-overview/scope.md`）。落地前至少先加 API Key 作为过渡。

## 7.2 Agent 可观测性

目标：监控 Agent 自身的运行质量（指标定义与 `00-overview/engineering-metrics.md` 对齐）。

| 指标 | 说明 |
| --- | --- |
| 诊断成功率 | succeeded / total_runs |
| 诊断延迟 P95 | 从 alert 入队到 report 生成 |
| 审批响应时间 | 从请求审批到决策完成 |
| Token 消耗趋势 | 按日/周汇总，按模型分拆 |
| 缓存命中率 | tool cache hit rate（per tool） |
| 误报率 | NFA / total_incidents |

## 7.3 A/B 评估框架

目标：安全地评估 Prompt/模型变更（评测集见 `09-evals/evaluation.md`）。

| 任务 | 细节 |
| --- | --- |
| 离线回放 | 历史 alert → 新 Prompt → 对比新旧结果 |
| Shadow Mode | 新版本并行运行，只看不执行，对比偏差 |
| 评估指标 | 根因准确率、证据充分度、动作合理性、NFA 率 |

## 7.4 高可用

目标：Agent 本身不能成为单点故障。

| 任务 | 细节 |
| --- | --- |
| API 多副本 | 无状态 API 水平扩展（`docker compose up --scale api=3`） |
| Worker 多副本 | Celery Worker 多实例，prefetch 调节 |
| PostgreSQL 主备 | Patroni / Cloud SQL HA |
| Redis Sentinel | 缓存和消息队列高可用 |
