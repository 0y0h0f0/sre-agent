# SRE Incident Response Agent — K8s 部署指南

> 部署后确认是否真正读到目标后端，请看 [K8s 后端对接验证](../../docs/08-deploy/k8s-backend-verification.md)。多服务和多后端边界见 [后端对接范围](../../docs/11-reference/backend-connectivity.md)。

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

# 4. 部署安全 base（fixture executor、M9 off、API auth on）
kubectl apply -k deploy/k8s/base/
```

> 公共镜像（`pgvector/pgvector`、`redis:7-alpine`、`prom/prometheus`、`grafana/grafana`、`grafana/loki`）使用默认的 `IfNotPresent` 策略。如果集群节点无法访问 Docker Hub，同样需要 `docker pull` + `docker save` + `scp` + `docker load` 预加载到各节点。

## 文件结构

```
deploy/k8s/
  README.md                        ← 本文件
  base/                            ← 基础清单（所有环境共用）
    namespace.yaml                 Namespace
    rbac.yaml                      ServiceAccount + namespace-scoped read-only Role/RoleBinding
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

Kustomize 通过 `secretGenerator` 读取 `base/.env.secret`。`base/secret.yaml` 只是字段参考，
不会被直接应用。先复制模板再填入真实连接信息：

```bash
cp deploy/k8s/base/.env.secret.example deploy/k8s/base/.env.secret
vim deploy/k8s/base/.env.secret
```

`.env.secret` 示例：

```dotenv
# Postgres — 指向你的 pgvector 数据库
DATABASE_URL=postgresql+psycopg://<db-user>:<db-password>@<pg-host>:5432/sre

# Redis — 指向你的 Redis 实例
REDIS_URL=redis://your-redis-host:6379/0
CELERY_BROKER_URL=redis://your-redis-host:6379/1
CELERY_RESULT_BACKEND=redis://your-redis-host:6379/2

# 如果启用 LLM
LLM_API_KEY=<replace-with-llm-api-key>

# 如果启用 SMTP 通知；SMTP_HOST 为空时只写 email_log skipped，不会真实发送
SMTP_HOST=smtp.example.com
SMTP_PORT=587
SMTP_TLS_MODE=starttls
SMTP_FROM=sre-agent@example.com
SRE_EMAIL_LIST=sre@example.com,oncall@example.com
WEB_BASE_URL=https://sre-agent.your-domain.com
SMTP_USER=sre-agent
SMTP_PASSWORD=<replace-with-smtp-password>

# 初始运维 API 密钥种子（用于生成第一个 operator key）
API_KEY_INITIAL_SEED=<replace-with-random-seed>
```

> **安全提示：** 生产环境请使用 Sealed Secrets、External Secrets Operator 或 Vault 管理 Secret，不要将明文 Secret 提交到 Git。

## 第三步：调整 ConfigMap（按需）

`base/configmap.yaml` 中的关键字段按你的环境调整：

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `PROMETHEUS_URL` | `prometheus.target-namespace.svc.cluster.local:9090` | 改为集群内实际 Prometheus 地址 |
| `LOKI_URL` | `loki.target-namespace.svc.cluster.local:3100` | 改为集群内实际 Loki 地址 |
| `BACKEND_URL_ALLOWLIST` | `*.svc.cluster.local,*.svc` | 后端 URL 安全白名单 |
| `K8S_NAMESPACE` | `target-namespace` | Agent 诊断的目标 namespace |
| `DEPLOYMENT_BACKEND` | `fixture` | 发布变更源；切到 `github` 时仍只读查询 deployments/commits |
| `GITHUB_REPO` | 空 | GitHub 仓库，格式为 `owner/repo`，仅 `DEPLOYMENT_BACKEND=github` 需要 |
| `GITHUB_API_URL` | `https://api.github.com` | GitHub API 地址；企业版可改为内部 API |
| `CORS_ALLOW_ORIGINS` | — | 前端域名 |
| `WEB_BASE_URL` | — | 前端 URL（用于邮件通知中的链接）|
| `SMTP_HOST` | 空 | 为空时邮件发送会被标记为 `skipped`；要收到真实邮件必须配置 |
| `SMTP_FROM` | `sre-agent@example.local` | 邮件发件人；真实 SMTP 通常要求使用被验证的域名 |
| `SRE_EMAIL_LIST` | `sre@example.local` | 逗号或分号分隔的全局收件人 |

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

# 检查 base RBAC 权限：只读应允许，写权限应拒绝
kubectl auth can-i get pods --as=system:serviceaccount:sre-agent:sre-agent
kubectl auth can-i list deployments --as=system:serviceaccount:sre-agent:sre-agent
kubectl auth can-i get statefulsets --as=system:serviceaccount:sre-agent:sre-agent
# → yes

kubectl auth can-i patch deployments --as=system:serviceaccount:sre-agent:sre-agent
kubectl auth can-i patch statefulsets --as=system:serviceaccount:sre-agent:sre-agent
# → no

# 创建第一个 API Key（用于后续操作）
kubectl exec -n sre-agent deployment/api -- python -c "
from apps.api.services.api_key_service import ApiKeyService
# 或通过 POST /api/keys 端点创建
"

# 检查数据库迁移
kubectl logs -n sre-agent deployment/api | grep "alembic"
```

### 验证邮件通知

邮件通知是 best-effort：诊断不会因为 SMTP 不可用而失败。没有收到邮件时，先查
`email_log`，再看 worker 日志。

```bash
# 使用内置 Postgres 时：
kubectl exec -n sre-agent statefulset/postgres -- \
  psql -U sre -d sre -c \
  "select notification_type,status,recipient_count,last_error,created_at \
   from email_log order by created_at desc limit 20;"

# 常见状态：
# queued  = 已写入邮件事件，但 send_email_notification 还没跑或 worker 没消费
# sent    = SMTP provider 已接受
# skipped = SMTP_HOST / SMTP_FROM / SRE_EMAIL_LIST 等前置配置缺失
# failed  = SMTP 连接、认证或发送错误；看 last_error 和 worker 日志

kubectl logs -n sre-agent deployment/worker --tail=200 | grep -i "email\\|smtp\\|notification"
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

如果需要 Agent 在审批后执行真实的 K8s 操作（Deployment/StatefulSet 重启、rollout pause/resume、Deployment 扩缩容/回滚），这是单独的高风险 opt-in。除了修改 ConfigMap，还必须在目标 namespace 创建独立、namespace-scoped 的写权限 Role/RoleBinding；base RBAC 不包含任何写权限。

```yaml
EXECUTOR_BACKEND: "live"
EXECUTOR_K8S_NAMESPACE: "your-target-namespace"
```

最小 Role 示例（应用到目标 namespace，subject 指向 `sre-agent` namespace 中的 ServiceAccount）：

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: sre-agent-live-executor
  namespace: your-target-namespace
rules:
  - apiGroups: ["apps"]
    resources: ["deployments"]
    verbs: ["get", "patch"]
  - apiGroups: ["apps"]
    resources: ["statefulsets"]
    verbs: ["get", "patch"]
  - apiGroups: ["apps"]
    resources: ["deployments/scale"]
    verbs: ["get", "patch"]
  - apiGroups: ["apps"]
    resources: ["deployments/rollback"]
    verbs: ["create"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: sre-agent-live-executor
  namespace: your-target-namespace
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: Role
  name: sre-agent-live-executor
subjects:
  - kind: ServiceAccount
    name: sre-agent
    namespace: sre-agent
```

> **注意：** Live executor 仍只允许现有 restart/pause/resume/scale/rollback Kubernetes mutation。StatefulSet 仅允许通过 pod template annotation 触发滚动重启，不允许 StatefulSet scale、PVC/storage 修改或任意 patch。L2 操作需审批，L3 操作需审批 + 二次确认；不要授予 cluster-wide 写权限。

## 可选：在集群已有可观测性时对接

```yaml
# base/configmap.yaml — 指向已有组件
PROMETHEUS_URL: "http://prometheus.target-namespace.svc.cluster.local:9090"
LOKI_URL: "http://loki.target-namespace.svc.cluster.local:3100"
METRICS_SERVICE_LABEL: "job"
LOGS_SERVICE_LABEL: "service"
JAEGER_URL: "http://jaeger.target-namespace.svc.cluster.local:16686"
TEMPO_URL: "http://tempo.target-namespace.svc.cluster.local:3200"
ALERTMANAGER_URL: "http://alertmanager.target-namespace.svc.cluster.local:9093"
ALERT_POLL_FILTER_MATCHERS: 'job=~\"checkout|payments|orders\"'
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
