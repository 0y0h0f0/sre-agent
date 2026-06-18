# 后端对接范围

**最后更新：** 2026-06-18

本文说明当前实现中的“后端对接”边界，避免把多服务诊断能力误解为多套后端并联能力。

各 backend adapter 的 worker 构造、`EffectiveConfig` 分界、cache bucket、URL safety 和 read-only diagnostics 细节见 [Observability 与后端适配器技术深挖](../00-overview/observability-backend-adapters-deep-dive.md)。服务发现、capability matrix、workload binding 和 topology 推导细节见 [Discovery、Capability Matrix 与服务拓扑技术深挖](../00-overview/discovery-capability-topology-deep-dive.md)。

## 当前模型

当前一个 Agent 部署实例对接一套后端环境。这套环境通常包含：

- 一个 Prometheus endpoint；
- 一个 Loki endpoint；
- 一个 trace backend，类型为 `fixture`、`jaeger`、`tempo` 或 `disabled`；
- 一个 deployment change backend，类型为 `fixture`、`github` 或 `argocd`；
- 一个 K8s API 访问上下文；
- 一个 Alertmanager endpoint；
- 一个可选 live read-only DB diagnostics endpoint。

在这一套环境内，Agent 可以诊断多个业务服务。服务识别依赖 alert labels、Prometheus/Loki service label、K8s discovery 和拓扑推导。

## 支持矩阵

| 场景 | 当前支持 | 说明 |
|------|----------|------|
| 同一 K8s 集群内多个业务服务 | 支持 | 通过 labels、discovery、topology 和告警 payload 区分服务 |
| 同一集群多个 namespace | 部分支持 | discovery 可用逗号分隔 `K8S_NAMESPACE` 扫描多个 namespace；单次 live K8s 诊断/执行仍应明确目标 namespace |
| 一个实例同时接多个 Prometheus | 不支持 | 当前配置是单值 `PROMETHEUS_URL` |
| 一个实例同时接多个 Loki | 不支持 | 当前配置是单值 `LOKI_URL` |
| 一个实例同时接多个 Jaeger/Tempo | 不支持 | `TRACE_BACKEND` 选择一个后端类型，URL 也是单值 |
| 一个实例同时接多个 GitHub repo 或 Argo CD 实例 | 不支持 | 当前 active deployment backend 是单个配置 |
| 一个实例同时接多个 K8s 集群 | 不支持 | live K8s 使用当前 in-cluster ServiceAccount 或 kubeconfig 上下文 |

## 推荐部署方式

如果多个业务服务共享同一套观测系统，部署一个 Agent 实例即可：

```text
业务服务 A/B/C
  -> 同一 Prometheus / Loki / Trace / Alertmanager / K8s API
  -> 一个 Agent 实例
```

如果是多套独立环境，优先选择每套环境一个 Agent 实例：

```text
prod-us     -> Agent A
prod-eu     -> Agent B
staging     -> Agent C
```

也可以先在观测层聚合，例如用 Thanos/Mimir、Loki gateway 或统一 trace gateway 暴露一个受控入口，再让 Agent 对接这个入口。聚合层需要保证 label、tenant、namespace 和权限边界清楚，否则诊断证据会混杂。

## 如果要原生多后端

当前代码不是 backend registry 模型。要支持一个 Agent 原生并联多套后端，至少需要新增：

- 后端 registry：按 cluster/tenant/environment 管理多组 Prometheus、Loki、trace、Alertmanager 和 K8s 配置。
- 路由策略：按 alert labels、service、namespace 或 cluster 选择后端。
- 权限边界：每个后端独立凭据、allowlist、审计和超时。
- 证据标识：每条 evidence 记录 backend/cluster/tenant 来源，避免跨环境误判。
- UI/API 表达：incident、agent run 和 discovery 页面展示所选后端。

这些属于新的架构切片，不应通过在现有单值配置里拼接多个 URL 来实现。

## 相关配置

| 配置 | 当前语义 |
|------|----------|
| `PROMETHEUS_URL` | 单个 Prometheus endpoint |
| `LOKI_URL` | 单个 Loki endpoint |
| `TRACE_BACKEND` | 单个 trace 后端类型 |
| `JAEGER_URL` / `TEMPO_URL` | 当前 trace 后端对应的单个 endpoint |
| `DEPLOYMENT_BACKEND` | 单个 deployment change 后端类型 |
| `GITHUB_REPO` / `ARGOCD_URL` | 当前 deployment backend 的单组配置 |
| `K8S_BACKEND` | `fixture` 或当前 K8s 上下文的 `live` |
| `K8S_NAMESPACE` | discovery allowlist；可逗号分隔多个 namespace |
| `EXECUTOR_K8S_NAMESPACE` | live executor 目标 namespace，应保持明确且受控 |
