# Demo 环境设计

## Docker Compose 服务

```text
api
worker
web
postgres
redis
prometheus
grafana
loki
otel-collector
demo-service
```

## 固定端口

| 服务 | 端口 |
| --- | --- |
| api | 8000 |
| web | 5173 |
| postgres | 5432 |
| redis | 6379 |
| prometheus | 9090 |
| grafana | 3000 |
| loki | 3100 |
| demo-service | 8080 |

## demo-service

接口：

```text
POST /faults/db-connection-exhaustion
POST /faults/high-5xx-after-deploy
POST /faults/cache-avalanche
POST /faults/pod-restart-loop
POST /faults/clear
GET  /healthz
GET  /metrics
```

要求：

- 暴露 Prometheus metrics。
- 输出结构化日志到 stdout，由 Loki 收集。
- 生成 mock trace 或 trace fixture。
- 故障注入后可通过 mock alert 触发诊断。

## 启动命令

```bash
docker compose up --build
```

初始化：

```bash
alembic upgrade head
python -m packages.rag.ingest --path demo/runbooks
```

触发 demo：

```bash
curl -X POST http://localhost:8080/faults/high-5xx-after-deploy
curl -X POST http://localhost:8000/api/alerts -H 'Content-Type: application/json' -d @demo/alerts/high-5xx.json
```

## 配置文件

```text
deploy/
  docker-compose.yml
  prometheus.yml
  loki.yml
  otel-collector.yml
  grafana/
    dashboards/
    datasources/
```

## 健康检查

- `api` 依赖 `/readyz`。
- `worker` 依赖 Redis 和 PostgreSQL。
- `demo-service` 依赖 `/healthz`。
- Prometheus scrape `api`、`worker`、`demo-service` metrics。

## 数据持久化

本地 demo 可使用 named volumes：

- `postgres_data`
- `grafana_data`
- `loki_data`

测试环境可以使用临时 volume，避免污染。
