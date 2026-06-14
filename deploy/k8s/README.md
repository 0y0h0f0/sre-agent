# SRE Incident Response Agent — K8s 部署指南

## 前置条件

| 条件 | 说明 |
|------|------|
| K8s 集群 ≥ 1.28 | 需要 Ingress、HPA（autoscaling/v2）支持 |
| kubectl 已配置 | 指向目标集群 |
| 容器镜像 | 本地 `docker build` → 加载到 K8s 节点（`imagePullPolicy: Never`），见下方 |
| PostgreSQL + pgvector | 推荐云托管，也可使用 `base/postgres.yaml` |
| Redis ≥ 7 | 推荐云托管，也可使用 `base/redis.yaml` |
| Ingress Controller | nginx-ingress（或其他，需修改 `ingress.yaml` 的 `ingressClassName`） |
| StorageClass | 用于 PVC（Prometheus/Loki/Grafana/Postgres/Redis），可选 |

## 镜像策略

所有自建镜像（`sre-agent`、`sre-agent-web`、`sre-agent-bge-zh`）的 Deployment 已配置 `imagePullPolicy: Never`，适用于以下流程：

```bash
# 1. 本地构建
docker build -t sre-agent:latest -f Dockerfile.prod .
docker build -t sre-agent-web:latest -f apps/web/Dockerfile apps/web
docker build -t sre-agent-bge-zh:latest -f deploy/bge-zh.Dockerfile .

# 2. 导出镜像
docker save sre-agent:latest sre-agent-web:latest sre-agent-bge-zh:latest -o sre-agent-images.tar

# 3. 加载到 K8s 节点
#    方式 A — 单节点（如 minikube / kind）：
minikube image load sre-agent:latest
minikube image load sre-agent-web:latest
#    kind load docker-image sre-agent:latest sre-agent-web:latest

#    方式 B — 多节点集群，每个节点执行：
scp sre-agent-images.tar node01:/tmp/
ssh node01 docker load -i /tmp/sre-agent-images.tar
#    对所有工作节点重复

# 4. 部署（镜像已在节点上，Never 策略下不会尝试远程拉取）
kubectl apply -k deploy/k8s/base/
```

> 公共镜像（`pgvector/pgvector`、`redis:7-alpine`、`prom/prometheus`、`grafana/grafana`、`grafana/loki`）使用默认的 `IfNotPresent` 策略。如果集群节点无法访问 Docker Hub，同样需要 `docker pull` + `docker save` + `scp` + `docker load` 预加载到各节点。

## 文件结构

```
deploy/k8s/
  README.md                        ← 本文件
  base/                            ← 基础清单（所有环境共用）
    namespace.yaml                 Namespace
    rbac.yaml                      ServiceAccount + ClusterRole + ClusterRoleBinding
    configmap.yaml                 非敏感环境变量（70+ 字段）
    secret.yaml                    Secret 模板（数据库/Redis/SMTP 密码等）
    api.yaml                       API Deployment + Service + HPA
    worker.yaml                    Worker Deployment + HPA
    beat.yaml                      Celery Beat Deployment（单副本）
    web.yaml                       Web 前端（nginx）Deployment + Service
    ingress.yaml                   Ingress 路由
    kustomization.yaml             Kustomize 聚合

    # 可选组件 — 按需取消注释 kustomization.yaml 中的引用：
    postgres.yaml                  Postgres StatefulSet + Service + PVC
    redis.yaml                     Redis StatefulSet + Service + PVC
    prometheus.yaml                Prometheus + ConfigMap + PVC
    loki.yaml                      Loki StatefulSet + ConfigMap + PVC
    grafana.yaml                   Grafana + ConfigMaps + PVC
    bge-zh.yaml                    BGE-ZH Embedding 服务 + PVC

  overlays/
    production/                    ← 生产环境覆盖
      kustomization.yaml
      configmap-patch.yaml         覆盖为生产安全默认值
      replica-patch.yaml           api/worker 扩至 3 副本
```

## 第一步：构建并加载镜像

所有自建镜像使用 `imagePullPolicy: Never`，需要先将镜像加载到各 K8s 节点。详见上方「镜像策略」章节。

## 第二步：配置 Secret

**必须修改 `base/secret.yaml`**，填入真实的连接信息：

```yaml
stringData:
  # Postgres — 指向你的 pgvector 数据库
  DATABASE_URL: "postgresql+psycopg://user:password@your-pg-host:5432/sre"

  # Redis — 指向你的 Redis 实例
  REDIS_URL: "redis://your-redis-host:6379/0"
  CELERY_BROKER_URL: "redis://your-redis-host:6379/1"
  CELERY_RESULT_BACKEND: "redis://your-redis-host:6379/2"

  # 如果启用 LLM
  LLM_API_KEY: "sk-your-key"

  # 如果启用 SMTP 通知
  SMTP_USER: "sre-agent"
  SMTP_PASSWORD: "your-smtp-password"

  # 初始运维 API 密钥种子（用于生成第一个 operator key）
  API_KEY_INITIAL_SEED: "your-random-seed"
```

> **安全提示：** 生产环境请使用 Sealed Secrets、External Secrets Operator 或 Vault 管理 Secret，不要将明文 Secret 提交到 Git。

## 第三步：调整 ConfigMap（按需）

`base/configmap.yaml` 中的关键字段按你的环境调整：

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `PROMETHEUS_URL` | `prometheus.task-platform.svc.cluster.local:9090` | 改为集群内实际 Prometheus 地址 |
| `LOKI_URL` | `loki.task-platform.svc.cluster.local:3100` | 改为集群内实际 Loki 地址 |
| `BACKEND_URL_ALLOWLIST` | `*.svc.cluster.local,*.svc` | 后端 URL 安全白名单 |
| `K8S_NAMESPACE` | `task-platform` | Agent 诊断的目标 namespace |
| `DEPLOYMENT_BACKEND` | `github` | 发布变更源；GitHub 后端只读查询 deployments/commits |
| `GITHUB_REPO` | `0y0h0f0/platform` | GitHub 仓库，格式为 `owner/repo` |
| `GITHUB_API_URL` | `https://api.github.com` | GitHub API 地址；企业版可改为内部 API |
| `CORS_ALLOW_ORIGINS` | — | 前端域名 |
| `WEB_BASE_URL` | — | 前端 URL（用于邮件通知中的链接）|

如果集群已有可观测性组件，注释掉 `base/kustomization.yaml` 中的 `prometheus.yaml`、`loki.yaml`、`grafana.yaml`。

## 第四步：部署

```bash
# 预览将要创建的资源
kubectl apply -k deploy/k8s/base/ --dry-run=client

# 部署基础组件
kubectl apply -k deploy/k8s/base/

# 或部署生产环境覆盖
kubectl apply -k deploy/k8s/overlays/production/ --dry-run=client
kubectl apply -k deploy/k8s/overlays/production/
```

## 第五步：验证

```bash
# 检查所有 Pod 状态
kubectl get pods -n sre-agent

# 检查 API 健康
kubectl port-forward -n sre-agent svc/api 8000:8000
curl http://localhost:8000/healthz
# → {"status":"ok"}

# 检查 Worker 连接
kubectl logs -n sre-agent deployment/worker --tail=20
# 应看到: celery@... ready.

# 检查 RBAC 权限
kubectl auth can-i get pods --as=system:serviceaccount:sre-agent:sre-agent
kubectl auth can-i list deployments --as=system:serviceaccount:sre-agent:sre-agent
kubectl auth can-i patch deployments --as=system:serviceaccount:sre-agent:sre-agent

# 创建第一个 API Key（用于后续操作）
kubectl exec -n sre-agent deployment/api -- python -c "
from apps.api.services.api_key_service import ApiKeyService
# 或通过 POST /api/keys 端点创建
"

# 检查数据库迁移
kubectl logs -n sre-agent deployment/api | grep "alembic"
```

## 第六步：暴露服务

### 方式 A：Ingress（推荐）

修改 `base/ingress.yaml` 中的 `host` 为实际域名，确保集群已部署 Ingress Controller：

```yaml
rules:
  - host: sre-agent.your-domain.com   # ← 改为你的域名
```

### 方式 B：NodePort / LoadBalancer（临时调试）

```bash
kubectl patch svc api -n sre-agent -p '{"spec":{"type":"LoadBalancer"}}'
kubectl patch svc web -n sre-agent -p '{"spec":{"type":"LoadBalancer"}}'
```

## 可选：添加可观测性

如果集群没有 Prometheus / Loki / Grafana，在 `base/kustomization.yaml` 中取消注释：

```yaml
resources:
  # ... base resources ...
  - postgres.yaml
  - redis.yaml
  - prometheus.yaml
  - loki.yaml
  - grafana.yaml
```

然后重新 apply。

## 可选：启用 Live Executor

如果需要 Agent 在审批后执行真实的 K8s 操作（重启 Pod、扩缩容、回滚），修改 ConfigMap：

```yaml
EXECUTOR_BACKEND: "live"
EXECUTOR_K8S_NAMESPACE: "your-target-namespace"
```

> **注意：** 确保 RBAC 中的 executor 权限范围已覆盖目标 namespace。L2 操作需审批，L3 操作需审批 + 二次确认。

## 可选：在集群已有可观测性时对接

```yaml
# base/configmap.yaml — 指向已有组件
PROMETHEUS_URL: "http://prometheus.task-platform.svc.cluster.local:9090"
LOKI_URL: "http://loki.task-platform.svc.cluster.local:3100"
METRICS_SERVICE_LABEL: "job"
LOGS_SERVICE_LABEL: "service"
JAEGER_URL: "http://jaeger.task-platform.svc.cluster.local:16686"
TEMPO_URL: "http://tempo.task-platform.svc.cluster.local:3200"
ALERTMANAGER_URL: "http://alertmanager.task-platform.svc.cluster.local:9093"
ALERT_POLL_FILTER_MATCHERS: 'job=~\"api-gateway|task-service-admin|user-service-admin\"'
```

然后**不需要**部署 `prometheus.yaml`、`loki.yaml`、`grafana.yaml`。

## 回滚

```bash
# 回滚 Deployment
kubectl rollout undo deployment/api -n sre-agent
kubectl rollout undo deployment/worker -n sre-agent

# 或完全删除
kubectl delete -k deploy/k8s/base/
```

## 常见问题

**Q: Worker 无法连接 Redis？**
确认 `secret.yaml` 中 `CELERY_BROKER_URL` 和 `REDIS_URL` 正确，且 Redis 可从集群内访问。

**Q: API 无法连接 K8s API？**
检查 ServiceAccount 和 RBAC：`kubectl describe sa sre-agent -n sre-agent`。

**Q: 前端页面空白？**
确认 web Deployment 的 nginx 配置中 `api` Service 名称正确（默认 `api:8000`），且 Ingress 路由 `/api` 路径优先级高于静态文件路由。

**Q: Alembic 迁移失败？**
手动运行：`kubectl exec -n sre-agent deployment/api -- alembic upgrade head`。
