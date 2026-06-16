# K8s 后端对接验证

**最后更新：** 2026-06-15

本文说明在 Kubernetes 部署后，如何确认 Agent 已经对接目标后端环境。完整部署步骤仍以 [K8s 部署指南](../../deploy/k8s/README.md) 为准；本文只覆盖验证路径。

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

检查 Agent ServiceAccount 的只读权限。把 `task-platform` 替换为实际目标 namespace：

```bash
kubectl auth can-i list pods -n task-platform \
  --as=system:serviceaccount:sre-agent:sre-agent

kubectl auth can-i get deployments.apps -n task-platform \
  --as=system:serviceaccount:sre-agent:sre-agent

kubectl auth can-i get pods/log -n task-platform \
  --as=system:serviceaccount:sre-agent:sre-agent
```

如果启用了 `EXECUTOR_BACKEND=live`，还要单独确认受控 patch/scale/rollback 权限；普通诊断路径只应依赖只读权限。

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

## 常见误判

| 现象 | 解释 |
|------|------|
| `/readyz` ready，但没有业务证据 | `/readyz` 不检查 Prometheus/Loki/K8s/Trace |
| discovery succeeded，但某个工具 degraded | discovery 证明发现到服务，不等于每类信号都有数据 |
| `K8S_NAMESPACE` 配了多个 namespace，但诊断只看一个 namespace | discovery 支持多 namespace 扫描；单次 K8s 诊断查询仍使用事件/服务对应的 namespace 配置 |
| `EXECUTOR_BACKEND=live` 可用 | 只代表受控 live executor 被显式启用，不代表可以执行任意 K8s 写操作 |
