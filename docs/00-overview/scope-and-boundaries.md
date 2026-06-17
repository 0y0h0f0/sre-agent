# 范围与安全边界

**最后更新：** 2026-06-15

## 当前范围

本项目维护一个已完成的 SRE Incident Response Agent，并包含若干受控生产化增强。默认运行路径仍是单租户、本地演示和 CI 友好的安全路径。

| 范围 | 当前能力 |
|------|----------|
| 告警摄取 | Webhook `POST /api/alerts`、Alertmanager 轮询、M9 gated Grafana webhook |
| 诊断 | Celery 异步触发 LangGraph；FakeLLM/真实 adapter；确定性 fallback |
| 证据 | Prometheus、Loki、fixture/Jaeger/Tempo traces、deployment、read-only K8s、read-only DB |
| Runbook | 摄取、chunk、512 维 embedding、BM25/vector/hybrid search、rerank、draft/review/version |
| Memory | L0 run、L1 incident、L2 service、L3 procedural memory；token budget 与 context compression |
| 审批 | L2/L3 人工审批，L3 二次确认，email token/batch/group 等协作面 |
| 执行 | 默认 fixture executor；显式 `EXECUTOR_BACKEND=live` 后仅支持有限 Kubernetes mutation |
| 报告 | Incident report 版本化生成和重新生成，不覆盖历史版本 |
| 配置发现 | 后端发现、proposal、review、publish、rollback；worker 仅读取已发布配置 |
| 前端 | React 控制台展示 incidents、runs、approvals、actions、reports、配置/发现和状态 |
| Eval/测试 | CI smoke eval 使用 FakeLLM；manual full eval 才允许真实 LLM |
| M9 | AI/Web/Tempo/Grafana/semantic search 在全局和子 feature flag 后面 |

## 默认安全姿态

- 本地默认：`APP_ENV=local`、`LLM_PROVIDER=fake`、fixture tools、`EXECUTOR_BACKEND=fixture`。
- 生产默认：未显式设置时 `LLM_PROVIDER=disabled`、discovery 默认关闭，executor 仍保持 fixture 默认值。
- CI、单元测试、集成测试和 smoke eval 必须使用 FakeLLM 与 fixture/mock execution。
- API 只创建记录并入队 Celery；不在请求中同步运行诊断图。
- 所有写 API 需要 request ID；错误响应使用统一结构。

## 读写边界

| 能力 | 默认行为 | 可选 live 行为 | 写入限制 |
|------|----------|----------------|----------|
| Metrics | Prometheus 查询或 fixture | Prometheus read API | 只读 |
| Logs | Loki 查询或 fixture | Loki read API | 只读 |
| Traces | fixture | Jaeger/Tempo read API | 只读 |
| Deployment | fixture | GitHub/Argo CD read adapter | 只读，不执行部署写入 |
| K8s diagnostics | fixture | describe/logs/events/rollout/get deployment/get statefulset | 只读 |
| DB diagnostics | fixture | 预定义 SELECT + read-only transaction + timeout | 只读，不修改应用数据库 |
| Executor | fixture | `LiveK8sExecutorBackend` | 仅允许已列出的 K8s mutation |
| LLM | FakeLLM/disabled | 手动启用 provider | 不授予执行权限；M9 只产出待审草稿 |
| Web search | disabled/fake | gated external provider | HTTPS/domain/size/timeout/redaction/audit |
| External embedding | disabled/fake/local BGE | gated external provider | 失败降级，不阻塞 runbook 入库 |

## 允许的真实写路径

只有 `EXECUTOR_BACKEND=live` 且动作通过 guardrail、审批和二次确认要求后，才允许 live executor 执行以下 Kubernetes mutation：

- `restart_pod` / `restart_service`：bounded irreversible rolling restart，通过 patch Deployment pod template 触发；不提供 restore/undo 保证。
- `restart_statefulset`：bounded irreversible rolling restart，通过 patch StatefulSet pod template 触发；要求执行前 snapshot 显示目标是 StatefulSet。
- `pause_rollout`：bounded irreversible rollout pause，通过 patch Deployment `spec.paused=true` 暂停 rollout；要求执行前 snapshot 显示 Deployment 尚未 paused。
- `resume_rollout`：bounded irreversible rollout resume，通过 patch Deployment `spec.paused=false` 恢复 rollout；要求执行前 snapshot 显示 Deployment 已 paused。
- `scale_deployment` / `scale_back`：通过 Deployment scale patch 调整副本数。
- `rollback_release`：调用 Deployment rollback subresource；`rollback_deployment` 是兼容别名，会规范化为同一操作。

这些写路径仍受 Kubernetes resource name 校验、namespace 限制、executor timeout、执行前 snapshot、执行后 verify/replan 和审计记录约束。执行后的 verify gates 只允许重新读取 metrics/logs、K8s rollout 状态和 DB read-only diagnostics；DB gate 不会触发数据库写入或新的 DB remediation。

## 禁止的真实写路径

以下行为不属于当前项目范围，不能通过文档、配置或模型输出绕过：

- 真实云资源写操作。
- 删除数据、修改应用数据库、truncate table。
- flush 真实 cache。
- 新增未记录的 Kubernetes mutation。
- 未经审批执行 L2/L3 动作。
- 把 L4 动作放入审批队列。
- 让 LLM 自动批准、自动发布、自动执行 remediation。
- 在 CI 稳定门禁中依赖真实 LLM 或不稳定外部服务。

## 风险等级

| 等级 | 示例 | 行为 |
|------|------|------|
| L0 | `query_metrics`、`query_logs`、`query_traces`、`query_git` | 只读，自动执行 |
| L1 | `create_ticket`、`generate_report`、`warmup_cache`、`adjust_connection_pool` | 低风险，自动执行 |
| L2 | `restart_pod`、`scale_deployment`、`restart_service`、`restart_statefulset`、`pause_rollout`、`resume_rollout`、`increase_memory_limit`、`scale_back`、`revert_config` | 需要人工审批 |
| L3 | `enable_rate_limit`、`raise_rate_limit`、`rollback_release`、`rollback_deployment`、`enable_circuit_breaker`、`switch_dns_resolver`、`failover`、`cancel_deployment` | 需要审批和二次确认 |
| L4 | `delete_data`、`truncate_table`、`flush_cache`、`modify_database` | 直接拒绝，永不执行 |

Unknown action 类型保守归类为 L2。动作类型、target 或 params 中出现 `delete`、`drop`、`truncate`、`modify_database`、`flush` 等禁止 token 时，直接升级为 L4。

L3 审批必须满足：

```text
risk_ack == true
confirm_action_type == action.type
confirm_target == action.target
```

## 配置与密钥边界

- Worker 仅读取已发布 `EffectiveConfigVersion`。
- Discovery 只填补空白，不覆盖显式配置。
- Override 必须有过期时间，最大 TTL 30 天。
- 配置优先级为 `env > active override > profile > published > safe default`。
- 原始密钥只通过环境变量或 SecretStr 引用；不得写入 DB、审计、日志、Agent state、prompt 或 report。
- 后端 URL 安全验证必须拒绝 production 中的 localhost、link-local、metadata endpoint 等危险目标。

## M9 安全不变量

- `M9_EXTENSIONS_ENABLED=false` 会强制关闭 M9 子能力；M8 Jaeger read backend 例外保留。
- 每个 M9 子能力必须有独立开关和回滚方式。
- LLM 只能生成 `RunbookDraft(status=pending_review)` 或 `AmendmentDraft(status=pending_review)`。
- Web search、外部 LLM、外部 embedding 等外部调用必须有 feature flag、timeout、redaction、audit/metric、error degradation 和 secret leakage test。
- Tempo discovery 在生产环境最多生成 `requires_review`，不能自动发布。
- Embedding 失败不能阻塞 runbook 摄取。
- M9 rollback 必须能恢复 `PRE_M9_TRACE_BACKEND` / `PRE_M9_TRACE_ENABLED`，不能硬编码为 fixture 或 jaeger。

## 非目标

- 多租户、RBAC/SSO 扩展或企业权限模型扩张。
- 模型 fine-tuning 或把真实 LLM 作为稳定 CI gate。
- 自动执行真实云变更。
- 自动执行 destructive data/cache/database 操作。
- 替代 L3+ 决策中的人工 SRE 判断。
- 把 roadmap 中未启用或未审查能力描述成默认可用能力。

## 需要停止并重新评估的情况

- 发现实现或文档会放宽上述写入边界。
- 发现原始密钥可能进入 DB、日志、审计、Agent state 或 prompt。
- 发现生产默认值会启用 M9 外部调用。
- 发现测试或 eval 需要真实 LLM 才能通过。
- 发现 LangGraph checkpoint 被业务 JSON snapshot 替代。
