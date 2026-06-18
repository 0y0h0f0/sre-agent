# K8s 后端对接验证

**最后更新：** 2026-06-18

本文说明在 Kubernetes 部署后，如何确认 Agent 已经对接目标后端环境。完整部署步骤仍以 [K8s 部署指南](../../deploy/k8s/README.md) 为准；本文只覆盖验证路径。

如果需要理解 Prometheus、Loki、Trace、Deployment、K8s 和 DB backend 在 worker 中如何构造、缓存和降级，见 [Observability 与后端适配器技术深挖](../00-overview/observability-backend-adapters-deep-dive.md)。如果要验证 `EXECUTOR_BACKEND=live` 的受控写路径、snapshot/preflight/verify 闭环和手动 API fixture-only 边界，见 [执行器、动作能力与验证闭环技术深挖](../00-overview/executor-action-verification-loop-deep-dive.md)。如果需要解释 discovery read API、capability matrix、topology、manual rerun lock 和 pending proposal 为什么不会自动进入 worker，见 [Discovery、Capability Matrix 与服务拓扑技术深挖](../00-overview/discovery-capability-topology-deep-dive.md)。

## 验证层级

不要只看 Pod Ready。推荐按三层确认：

| 层级 | 目的 | 通过标准 |
|------|------|----------|
| 配置下发 | API/worker 读取到目标后端配置 | 环境变量指向真实后端地址，不是空值或意外 localhost |
| 基础连通 | API 依赖和 K8s RBAC 可用 | `/readyz` ready，ServiceAccount 能读目标 namespace |
| 实际读取 | Agent run 产生真实工具调用 | `tool_calls` 中 metrics/logs/traces/k8s 等工具成功或可解释 degraded |

## 1. 配置下发

查看当前 ConfigMap：

```bash
kubectl -n sre-agent get cm sre-agent-config -o yaml
```

分别确认 API 和 worker 容器里的运行时环境变量：

```bash
kubectl -n sre-agent exec deploy/api -- sh -lc \
'printenv | sort | egrep "PROMETHEUS_URL|LOKI_URL|JAEGER_URL|TEMPO_URL|ALERTMANAGER_URL|K8S_BACKEND|K8S_NAMESPACE|TRACE_BACKEND|DEPLOYMENT_BACKEND|DB_DIAGNOSTICS_BACKEND|EXECUTOR_BACKEND"'

kubectl -n sre-agent exec deploy/worker -- sh -lc \
'printenv | sort | egrep "PROMETHEUS_URL|LOKI_URL|JAEGER_URL|TEMPO_URL|ALERTMANAGER_URL|K8S_BACKEND|K8S_NAMESPACE|TRACE_BACKEND|DEPLOYMENT_BACKEND|DB_DIAGNOSTICS_BACKEND|EXECUTOR_BACKEND"'
```

期望看到 Prometheus、Loki、Jaeger/Tempo、Alertmanager 等 URL 指向目标环境的真实服务地址，例如 `*.svc` 或 `*.svc.cluster.local`。生产环境不要把未授权的 localhost、metadata endpoint 或任意私网地址作为后端证据源。

## 2. 基础连通

检查 API readiness：

```bash
kubectl -n sre-agent port-forward svc/api 8000:8000
curl http://localhost:8000/readyz
```

`/readyz` 只验证 Postgres、Redis 和 Celery broker，不代表 Prometheus/Loki/K8s 业务后端已可读。

检查 Agent ServiceAccount 的只读权限。把 `target-namespace` 替换为实际目标 namespace：

```bash
kubectl auth can-i list pods -n target-namespace \
  --as=system:serviceaccount:sre-agent:sre-agent

kubectl auth can-i get deployments.apps -n target-namespace \
  --as=system:serviceaccount:sre-agent:sre-agent

kubectl auth can-i get statefulsets.apps -n target-namespace \
  --as=system:serviceaccount:sre-agent:sre-agent

kubectl auth can-i get pods/log -n target-namespace \
  --as=system:serviceaccount:sre-agent:sre-agent
```

如果启用了 `EXECUTOR_BACKEND=live`，还要单独确认受控 Deployment/StatefulSet patch、Deployment scale/rollback 权限；普通诊断路径只应依赖只读权限。

这里的 RBAC 检查只能证明 ServiceAccount 权限具备条件。一次真实 action 是否会调用 Kubernetes 写 API，还取决于 guardrail、L2/L3 审批、L3 二次确认、pre-action snapshot、action capability metadata、params whitelist 和 post-action verify。

## 3. Discovery 状态

如果 `API_KEY_AUTH_ENABLED=true`，以下请求需要加 `Authorization: Bearer <api_key>`。

```bash
curl http://localhost:8000/api/discovery/status
curl http://localhost:8000/api/discovery/services
curl http://localhost:8000/api/discovery/metrics
curl http://localhost:8000/api/discovery/topology
curl http://localhost:8000/api/discovery/capabilities
```

必要时手动触发一次发现：

```bash
curl -X POST http://localhost:8000/api/discovery/rerun \
  -H "Content-Type: application/json" \
  -d '{"triggered_by":"operator"}'
```

通过标准：

- `/api/discovery/status` 的 `latest_run.status` 是 `succeeded`，或是原因清楚的 `degraded`。
- `total_services_discovered` 大于 0。
- `/api/discovery/services` 能看到目标服务。
- `/api/discovery/capabilities` 中对应服务的 `metrics_available`、`logs_available`、`traces_available`、`k8s_accessible` 与实际接入能力一致。

## 4. Agent Run 证据

最终以一次真实告警后的 Agent run 为准：

```bash
curl http://localhost:8000/api/agent-runs/<agent_run_id>
```

查看响应里的 `tool_calls`：

| 工具名 | 说明 |
|--------|------|
| `metrics` | 读取 Prometheus |
| `logs` | 读取 Loki |
| `traces` | 读取 Jaeger 或 Tempo |
| `git_changes` | 读取 fixture/GitHub/Argo CD 发布变更 |
| `k8s` | 读取 Kubernetes 诊断 |
| `db_diagnostics` | 读取 fixture/live read-only DB 诊断 |

`succeeded` 表示该工具成功读取并产生证据。`degraded` 不一定是部署失败，可能表示该服务没有对应日志、trace、metric 或权限不足；需要结合 `output_summary` 和 `error_message` 判断。

## 5. Live Executor Smoke

`EXECUTOR_BACKEND=live` 只能在非生产或受控预生产 namespace 做 smoke。不要在真实业务生产 namespace 首次验证 live 写路径。

仓库提供了一个受控脚本入口：

```bash
# 只读 preflight，检查配置、RBAC、目标资源和 API health
MODE=preflight \
TARGET_NS=sre-agent-smoke \
DEPLOYMENT=checkout \
scripts/k8s_live_executor_smoke.sh

# 推荐：事务模式会临时创建 checkout、切 live/fake、执行 scale smoke，并自动恢复配置
MODE=agent-scale-transaction \
TARGET_NS=target-namespace \
DEPLOYMENT=checkout \
LIVE_EXECUTOR_SMOKE_CONFIRM=agent-scale-transaction:target-namespace:checkout \
scripts/k8s_live_executor_smoke.sh
```

`agent-scale-transaction` 模式依赖 FakeLLM 的 `CPUThrottling -> scale_deployment(checkout, replicas=4)` 固定计划，会创建临时 `checkout` Deployment、临时切换 `EXECUTOR_BACKEND=live` 与 `LLM_PROVIDER=fake`、执行 smoke，然后恢复原始 Agent 配置、删除临时 Deployment、撤销临时 API key。裸 `agent-scale` 只适合已经手动准备好受控目标和运行时配置的环境。其它 live action 仍按下面的手动 smoke 顺序验证。

前置条件：

- 使用受控 namespace，例如 `sre-agent-smoke`，其中只有专用 Deployment 和 StatefulSet。
- API/worker 都显式配置 `EXECUTOR_BACKEND=live`、`K8S_BACKEND=live`。
- `K8S_NAMESPACE` 和 `EXECUTOR_K8S_NAMESPACE` 指向同一个受控 namespace，或明确包含该 namespace。
- 使用 `LLM_PROVIDER=fake` 或 `disabled`，不要让真实 LLM 决定 smoke 行为。
- 非 K8s 写类外部后端继续使用 fixture 或只读 adapter。
- 保留审批流程；不要绕过 L2/L3 approval、snapshot、preflight 和 verify。

额外 RBAC 检查：

```bash
kubectl auth can-i patch deployments.apps -n sre-agent-smoke \
  --as=system:serviceaccount:sre-agent:sre-agent

kubectl auth can-i patch deployments.apps/scale -n sre-agent-smoke \
  --as=system:serviceaccount:sre-agent:sre-agent

kubectl auth can-i create deployments.apps/rollback -n sre-agent-smoke \
  --as=system:serviceaccount:sre-agent:sre-agent

kubectl auth can-i patch statefulsets.apps -n sre-agent-smoke \
  --as=system:serviceaccount:sre-agent:sre-agent
```

`deployments/rollback` 还需要 Kubernetes API discovery 支持。部分较新的集群不再暴露该子资源，即使 `kubectl auth can-i create deployments.apps/rollback` 显示 `yes`，真实调用仍会返回 `NotFound`。用下面的 discovery 结果确认：

```bash
kubectl get --raw /apis/apps/v1 | jq -r \
  '.resources[] | select(.name | test("^(deployments|statefulsets)(/|$)")) | .name + " " + (.verbs | join(","))'
```

如果输出中没有 `deployments/rollback`，当前集群不能通过现有 live executor 的 rollback subresource 路径验证 `rollback_release`；其它 live 写路径仍可按下列 smoke 顺序验证。

这些权限只覆盖当前受控 live executor 支持的 Kubernetes mutation。仍然不允许云资源写操作、应用数据库写操作、truncate、delete、flush cache 或任意 ad hoc 命令。

执行路径必须经过真实 Agent run。`POST /api/actions/{action_id}/execute` 是手动 fixture-only 边界，不能用它证明 live executor 已经实际调用 Kubernetes 写 API。

推荐 smoke 顺序：

1. `pause_rollout`：审批 L2 action 后，确认目标 Deployment 的 `.spec.paused=true`，Agent verify 将 paused 视为该 action 的成功信号。
2. `resume_rollout`：审批 L2 action 后，确认 `.spec.paused=false`，verify 不应把 paused 状态继续视为 resolved。
3. `scale_deployment`：使用显式 `replicas` 参数，确认 Deployment desired replicas 变为目标值。
4. `scale_back`：使用 action snapshot 或显式原副本数，确认 Deployment 回到预期副本数。
5. `restart_deployment` / `restart_service` / `restart_pod`：确认 Deployment pod template annotation 更新，并且 rollout 完成。
6. `restart_statefulset`：确认 StatefulSet pod template annotation 更新，并且 revision/ready 状态推进到完成。
7. `rollback_release`：按 L3 流程审批，必须包含 `risk_ack=true`、`confirm_action_type` 和 `confirm_target`；确认 Deployment rollback subresource 被调用且 rollout 完成。

每一步都保存以下证据，方便之后对照审计和回滚：

- approval ID、action ID、agent run ID。
- `execution_result.status`、`execution_result.backend`、`execution_result.details`。
- 对应 node trace 和 tool call 记录。
- action 前后的 `kubectl get deploy <name> -n sre-agent-smoke -o yaml` 或 `kubectl get sts <name> -n sre-agent-smoke -o yaml`。
- 最终 incident report。

负向 smoke 也要覆盖：

- 非法 target 或 namespace mismatch 必须在 preflight 阶段失败，不能调用 Kubernetes 写 API。
- action params 中未允许字段必须失败。
- `delete_data`、`truncate_table`、`flush_cache`、`modify_database` 必须直接按 L4 拒绝。
- snapshot 与当前资源不匹配时必须拒绝或 degraded，不能盲目执行。

live smoke 结束后的安全回滚：

```bash
export EXECUTOR_BACKEND=fixture
export K8S_BACKEND=fixture
docker compose restart api worker
```

如果部署在 Kubernetes 中，通过对应 Deployment/Helm/Kustomize 配置回滚这些环境变量，并重启 API 与 worker Pod。

## 常见误判

| 现象 | 解释 |
|------|------|
| `/readyz` ready，但没有业务证据 | `/readyz` 不检查 Prometheus/Loki/K8s/Trace |
| discovery succeeded，但某个工具 degraded | discovery 证明发现到服务，不等于每类信号都有数据 |
| `K8S_NAMESPACE` 配了多个 namespace，但诊断只看一个 namespace | discovery 支持多 namespace 扫描；单次 K8s 诊断查询仍使用事件/服务对应的 namespace 配置 |
| `EXECUTOR_BACKEND=live` 可用 | 只代表受控 live executor 被显式启用，不代表可以执行任意 K8s 写操作 |
