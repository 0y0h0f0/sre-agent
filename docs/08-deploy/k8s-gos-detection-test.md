# gos K8s 检测验收

**最后更新：** 2026-06-23

本文说明如何在 Kubernetes 中验证 SRE Agent 是否能对接并检测 `~/gos` 的
`task-platform` 后端项目。脚本入口：

```bash
scripts/k8s_gos_detection_smoke.sh
```

该脚本不运行 `pytest`、前端测试或 Playwright。它通过 K8s、业务流量、
`POST /api/alerts`、Discovery API 和 Agent run 的 `tool_calls` 做手动集成验收。

## 前置条件

- `kubectl` 已指向测试集群。
- `~/gos` 已部署到 `task-platform` namespace。
- Agent 已部署到 `sre-agent` namespace。
- 本机有 `kubectl`、`curl`、`jq`、`python3`、`base64`。
- Agent API 可通过 API key 访问，或可以提供初始 bootstrap seed。

推荐先部署 `~/gos`：

```bash
cd ~/gos
./deploy/k8s/deploy-all.sh dev --build
```

再部署 Agent：

```bash
cd /home/yhf/agentp
kubectl apply -k deploy/k8s/base/
```

## 首次准备

首次对接 `~/gos` 时执行：

```bash
cd /home/yhf/agentp
export SRE_AGENT_API_KEY="<operator-api-key>"
# 或仅在还没有任何 API key 时使用：
# export SRE_AGENT_BOOTSTRAP_SEED="<API_KEY_INITIAL_SEED>"

scripts/k8s_gos_detection_smoke.sh --setup-only
```

`--setup-only` 会：

- 在 `task-platform` 中创建只读 Role/RoleBinding，允许 `sre-agent:sre-agent` 读取 Pod、日志、事件、Deployment、Service 等。
- 将 Agent ConfigMap patch 为读取 `task-platform` 的 Prometheus、Loki、Jaeger、Alertmanager 和 K8s API。
- 保持 `EXECUTOR_BACKEND=fixture`，不启用真实 remediation 写入。
- 设置 `K8S_BACKEND=live`、`TRACE_BACKEND=jaeger`。
- 默认保持 `DB_DIAGNOSTICS_BACKEND=fixture`。
- 给 `api-gateway`、`user-service`、`task-service` 开启 `ENABLE_SRE_FAULTS=true`。

如果要覆盖 live DB read-only diagnostics，使用只读账号 DSN：

```bash
export SRE_AGENT_DB_DIAGNOSTICS_URL='postgresql://readonly:***@postgres.task-platform.svc.cluster.local:5432/task_platform?sslmode=disable'
scripts/k8s_gos_detection_smoke.sh --setup-only
```

DSN 会 patch 到 Agent Secret，不写入 ConfigMap。

## 全量场景

运行默认全量 smoke：

```bash
scripts/k8s_gos_detection_smoke.sh --setup
```

默认场景：

| 场景 | 覆盖 |
|------|------|
| `latency_spike` | 应用延迟、metrics/logs/traces、DB diagnostics、L1/L2 规划 |
| `error_burst` | 5xx、L3 审批等待路径 |
| `db_dependency` | Postgres 不可用、DB 相关诊断 |
| `redis_failure` | Redis 依赖故障、缓存类诊断 |
| `user_service_down` | user-service 下游故障 |
| `task_service_down` | task-service 下游故障 |
| `pod_restart` | K8s rollout/restart 事件、K8s live diagnostics |
| `metrics_unavailable` | `/metrics` 盲区、Prometheus degraded |
| `tracing_disabled` | tracing_enabled=0、trace degraded/缺失信号 |
| `prometheus_down` | Prometheus 后端不可用时的降级 |
| `catalog_alerts` | 扩展故障分类的 FakeLLM/规则路径和工具查询模板 |

只跑部分场景：

```bash
scripts/k8s_gos_detection_smoke.sh \
  --scenarios latency_spike,error_burst,pod_restart
```

缩短场景时间：

```bash
scripts/k8s_gos_detection_smoke.sh --scenario-seconds 60 --warmup-seconds 15
```

如果你已经自己做好 port-forward：

```bash
export SRE_AGENT_URL=http://127.0.0.1:8000
export GOS_BASE_URL=http://127.0.0.1:8080
scripts/k8s_gos_detection_smoke.sh --no-port-forward
```

## 输出

每次运行写入：

```text
reports/k8s-gos-detection/<timestamp>/
```

关键文件：

- `summary.jsonl`：每个 discovery/scenario 一行，包含 run 状态、缺失工具、失败工具、降级工具和 tool_call 摘要。
- `<scenario>/alert.json`：发给 Agent 的告警。
- `<scenario>/alert-response.json`：Incident 和 Agent run ID。
- `<scenario>/agent-run.json`：完整 Agent run 详情。
- `<scenario>/traffic.log`：业务流量脚本输出。
- `discovery/*.json`：Discovery API 响应。
- `agent-worker-env.txt`：worker 中关键后端配置快照。
- `agent-worker-k8s-incluster.txt`：worker Pod 内 Kubernetes in-cluster 配置快照，包括 `KUBERNETES_SERVICE_HOST` 和 service account token/CA 是否可读。
- `can-i-*.txt`：ServiceAccount RBAC 检查结果。

建议先看：

```bash
jq . reports/k8s-gos-detection/<timestamp>/summary.jsonl
```

通过标准：

- discovery 至少能发现 `task-platform` 服务，或清楚标记 degraded 原因。
- 关键 Agent run 状态为 `succeeded` 或 `waiting_approval`。
- `missing_tools` 为空。
- `failed_tools` 为空。
- 允许 `metrics`、`traces`、`k8s` 等在故意制造盲区时出现 `degraded`，但需要能解释原因。

如果 `k8s` 变成 `degraded`，优先检查：

```bash
cat reports/k8s-gos-detection/<timestamp>/agent-worker-k8s-incluster.txt
cat reports/k8s-gos-detection/<timestamp>/can-i-list-events.txt
cat reports/k8s-gos-detection/<timestamp>/can-i-list-events-events-k8s-io.txt
```

Pod 内缺少 `KUBERNETES_SERVICE_HOST` 或 service account token 时，live K8s diagnostics 无法初始化 Kubernetes client；这通常是 ServiceAccount token 未挂载或 Pod 模板禁用了自动挂载导致的。

## 安全边界

脚本会对测试 namespace 做这些 K8s 写操作：

- patch Agent ConfigMap/Secret。
- 创建只读 RBAC。
- `rollout restart` Agent API/worker 和部分 `~/gos` Deployment。
- 给 `~/gos` Deployment 设置/清理故障环境变量。
- 临时 scale `postgres`、`redis`、`user-service`、`task-service`、`prometheus`。

脚本不会：

- 启用 `EXECUTOR_BACKEND=live`。
- 通过 Agent 执行真实 Kubernetes remediation。
- 删除数据、truncate 表、flush cache。
- 调用真实 LLM。

退出时会尽力清理应用内故障并恢复记录过的副本数。Agent 的只读 RBAC 和 live-read 配置属于测试准备项，不会自动回滚。
