# 演示操作手册

**最后更新：** 2026-06-14

本手册用于演示完整事件响应闭环：告警进入 API，Celery worker 运行 LangGraph，工具层收集证据，Agent 生成根因和动作，guardrail 分类风险，L2/L3 等待人工审批，执行 fixture action，验证并生成报告。

## 准备

启动完整本地技术栈：

```bash
docker compose up -d
```

可选：启用邮件测试 UI。

```bash
docker compose --profile dev up -d
```

确认健康状态：

```bash
curl http://localhost:8000/healthz
curl http://localhost:8000/readyz
curl http://localhost:8080/healthz
```

录入 runbook：

```bash
curl -X POST http://localhost:8000/api/runbooks/ingest   -H "Content-Type: application/json"   -d '{"path":"demo/runbooks","reingest":true}'
```

打开控制台：`http://localhost:5173`。

Compose 默认 `API_KEY_AUTH_ENABLED=false`。如果你临时开启认证，先在左侧认证面板设置 API key，或在 curl 请求中加 `Authorization: Bearer <api_key>`。

## 演示流程

1. 选择一个场景。
2. 调用 demo-service fault endpoint 产生 metrics/logs。
3. 向 `/api/alerts` 提交对应 `demo/alerts/*.json`。
4. 在 `/incidents` 打开事件详情。
5. 打开最近的 Agent Run，观察节点轨迹、工具调用、证据网络、cache/token/compression 展示。
6. 如果事件进入 `waiting_approval`，在 `/approvals` 完成审批。
7. 回到事件详情或报告页查看执行结果和报告。
8. 演示结束后调用 `/faults/clear` 清理 demo-service 内存状态。

清理 fault：

```bash
curl -X POST http://localhost:8080/faults/clear
```

## 场景一：部署后高 5xx

注入故障并提交告警：

```bash
curl -X POST http://localhost:8080/faults/high-5xx-after-deploy

curl -X POST http://localhost:8000/api/alerts   -H "Content-Type: application/json"   -d @demo/alerts/high-5xx.json
```

| 项 | 预期 |
|----|------|
| `alert_name` | `High5xxAfterDeploy` |
| 主要证据 | Prometheus 5xx、Loki 错误日志、deployment fixture、trace fixture、runbook chunk |
| 根因方向 | 最近部署引入回归 |
| 常见动作形态 | 回滚、创建 ticket |
| 审批形态 | 回滚类动作通常为 L3，需要二次确认；ticket 为 L1 自动执行 |

演示重点：在 Agent Run 页展示 signal swimlanes 和 deployment 相关证据；在审批弹窗演示 L3 的 `risk_ack`、`confirm_action_type`、`confirm_target`。

## 场景二：Redis 缓存雪崩

```bash
curl -X POST http://localhost:8080/faults/cache-avalanche

curl -X POST http://localhost:8000/api/alerts   -H "Content-Type: application/json"   -d @demo/alerts/cache-avalanche.json
```

| 项 | 预期 |
|----|------|
| `alert_name` | `RedisCacheAvalanche` |
| 主要证据 | Redis 命中率下降、DB 连接压力、缓存雪崩日志、cache runbook |
| 根因方向 | 大量 key 过期或缓存 miss storm 导致 DB 压力上升 |
| 常见动作形态 | 扩容缓存、启用保护/熔断、创建后续项 |
| 审批形态 | 运维变更通常为 L2；熔断/限流类可能升级为 L3 |

演示重点：展示多信号关联，而不是只看单条告警。报告里应保留 metrics/logs/runbook 的 evidence references。

## 场景三：DB 连接耗尽

```bash
curl -X POST http://localhost:8080/faults/db-connection-exhaustion

curl -X POST http://localhost:8000/api/alerts   -H "Content-Type: application/json"   -d @demo/alerts/db-connection-exhaustion.json
```

| 项 | 预期 |
|----|------|
| `alert_name` | `DatabaseConnectionExhaustion` |
| 主要证据 | DB active connections、连接池耗尽日志、DB diagnostics fixture、runbook chunk |
| 根因方向 | 慢查询积压或 idle-in-transaction 导致连接池耗尽 |
| 常见动作形态 | 调整连接池、处理 idle transaction、创建调查项 |
| 审批形态 | 未知或数据库相关动作会被 guardrail 保守处理，通常需要 L2 审批；真实 DB 写操作仍禁止 |

演示重点：说明 live DB diagnostics 即使启用也只能跑预定义 SELECT，系统不允许修改应用数据库、truncate 表或清空数据。

## 场景四：Pod 重启循环

```bash
curl -X POST http://localhost:8080/faults/pod-restart-loop

curl -X POST http://localhost:8000/api/alerts   -H "Content-Type: application/json"   -d @demo/alerts/pod-restart-loop.json
```

| 项 | 预期 |
|----|------|
| `alert_name` | `PodRestartLoop` |
| 主要证据 | 内存指标、pod restart counter、K8s fixture event、OOMKilled 日志 |
| 根因方向 | 内存泄漏或内存 limit 不足导致 OOMKilled 和重启循环 |
| 常见动作形态 | 提高资源、重启/扩缩容、回滚 |
| 审批形态 | Kubernetes 运维动作为 L2；回滚类为 L3 |

演示重点：展示 K8s 诊断是只读的。默认 fixture executor 不会真实修改集群。

## 审批演示

推荐使用 React 控制台完成审批，因为 UI 会按风险等级展示必要字段。

### L2 审批

在 `/approvals` 打开 waiting approval，填写审批人和可选备注后批准或驳回。

API 示例：

```bash
curl -X POST http://localhost:8000/api/approvals/<approval_id>/approve   -H "Content-Type: application/json"   -d '{"approver":"demo-sre","comment":"approved for demo"}'
```

驳回必须写 comment：

```bash
curl -X POST http://localhost:8000/api/approvals/<approval_id>/reject   -H "Content-Type: application/json"   -d '{"approver":"demo-sre","comment":"not enough evidence"}'
```

### L3 二次确认

L3 不能只点批准。必须从 action 详情或 UI 中确认 action type 和 target，并原样提交：

```bash
curl -X POST http://localhost:8000/api/approvals/<approval_id>/approve   -H "Content-Type: application/json"   -d '{
    "approver":"demo-sre",
    "comment":"rollback approved for demo",
    "risk_ack":true,
    "confirm_action_type":"<action.type>",
    "confirm_target":"<action.target>"
  }'
```

批量审批只适合 L2 或低风险演示路径。批量批准不会自动补 L3 二次确认字段，L3 应单独打开审批弹窗处理。

## 查看结果

| 入口 | 用途 |
|------|------|
| `http://localhost:5173/incidents` | 事件列表和筛选 |
| `/incidents/:incidentId` | 事件详情、证据、动作、审批、评论、审计 |
| `/agent-runs/:agentRunId` | LangGraph 节点轨迹、工具调用、可视化、token/context/compression |
| `/approvals` | waiting/approved/rejected/expired 审批列表 |
| `/incidents/:incidentId/report` | 事件报告和重新生成 |
| `http://localhost:3000` | Grafana dashboard |
| `http://localhost:8025` | Mailpit，需 `--profile dev` |

报告生成后，重点检查：

- 根因有 evidence id 或 runbook chunk 引用。
- 动作列表显示审批和执行状态。
- L3 审批记录保留二次确认字段。
- Regenerate 报告会创建新版本，不覆盖旧版本。

## 扩展 FakeLLM 场景

除 4 个现成 JSON fixture 外，FakeLLM 还支持以下 `alert_name`：

- `CPUThrottling`
- `MemoryLeak`
- `DiskFull`
- `CertificateExpiry`
- `DNSFailure`
- `MessageQueueLag`
- `RateLimitTriggered`
- `SlowAPI`
- `ErrorBudgetBurn`
- `P0SiteOutage`
- `DownstreamTimeout`

可以复制任意 `demo/alerts/*.json`，修改 `fingerprint` 和 `alert_name` 后提交。未知 `alert_name` 会回退到 `High5xxAfterDeploy` 诊断路径。扩展场景没有对应 demo-service fault endpoint 时，工具证据可能更依赖 fixture 和 runbook；这属于可接受的确定性演示行为。

## M9 演示

M9 能力默认关闭。只在手动演示或专项验证时开启相关 flag，并且每个能力都要独立回滚。

示例：启用 LLM runbook 草稿生成。

```bash
export M9_EXTENSIONS_ENABLED=true
export RUNBOOK_LLM_GENERATION_ENABLED=true
docker compose restart api worker
```

M9 的不变量仍然生效：LLM 只能生成 `pending_review` 草稿，不能自动发布、自动审批、自动执行；Web/LLM/external embedding 调用必须经过 feature gate、超时、脱敏、审计、指标和降级。

详细 M9 流程见 [M9 发布计划](../m9-rollout.md)。

## 常见演示问题

| 问题 | 处理 |
|------|------|
| 提交同一个 fixture 没有新事件 | `fingerprint` 会去重 open incident；修改 fingerprint 或关闭/完成旧事件后再试 |
| 事件一直 queued/running | 看 `docker compose logs -f worker` 和 Redis 连接配置 |
| 没有审批项 | 可能动作是 L0/L1 自动执行，或 L4 被直接拒绝；查看事件动作列表和 Agent Run |
| L3 批量批准失败 | 这是预期安全行为；打开单个审批并填写二次确认字段 |
| 报告页 404 | 诊断尚未结束或报告未生成；等待 run 完成或点击重新生成 |
| 证据不够丰富 | 先调用对应 demo-service fault endpoint，再提交告警；确认 runbook 已 ingest |
