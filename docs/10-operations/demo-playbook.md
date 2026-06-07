# Demo 演示流程

## 目标

演示从告警到诊断、审批、mock 执行和报告的完整闭环。

## 准备

启动本地栈：

```bash
docker compose up -d
```

确认 API ready：

```bash
curl http://localhost:8000/readyz
```

入库 Runbook：

```bash
curl -X POST http://localhost:8000/api/runbooks/ingest \
  -H "Content-Type: application/json" \
  -H "X-Request-Id: req_demo_runbooks" \
  -d '{"path":"demo/runbooks","reingest":true}'
```

打开控制台：

```text
http://localhost:5173/incidents
```

## 场景 1：High 5xx After Deploy

发送告警：

```bash
curl -X POST http://localhost:8000/api/alerts \
  -H "Content-Type: application/json" \
  -H "X-Request-Id: req_high_5xx" \
  --data @demo/alerts/high-5xx.json
```

预期：

- 创建 incident 和 agent run。
- Agent 收集 metrics/logs/traces/deployment/runbook。
- FakeLLM 诊断部署回归。
- 推荐 `rollback_release`，风险 L3。
- 进入 waiting approval。
- 前端 Approvals 页面显示 L3 二次确认。

审批 L3 时需要输入 action type 和 target。

## 场景 2：Redis Cache Avalanche

```bash
curl -X POST http://localhost:8000/api/alerts \
  -H "Content-Type: application/json" \
  -H "X-Request-Id: req_cache_avalanche" \
  --data @demo/alerts/cache-avalanche.json
```

预期：

- 诊断同步 TTL 或 hot key 问题。
- `warmup_cache` 为 L1，可自动 mock 执行。
- `enable_rate_limit` 为 L3，等待审批。

## 场景 3：Database Connection Exhaustion

```bash
curl -X POST http://localhost:8000/api/alerts \
  -H "Content-Type: application/json" \
  -H "X-Request-Id: req_db_conn" \
  --data @demo/alerts/db-connection-exhaustion.json
```

预期：

- 诊断连接池耗尽。
- 推荐 `adjust_connection_pool` 和 `create_ticket`。
- L1 mock 执行或记录。
- 生成报告。

## 场景 4：Pod Restart Loop

```bash
curl -X POST http://localhost:8000/api/alerts \
  -H "Content-Type: application/json" \
  -H "X-Request-Id: req_pod_restart" \
  --data @demo/alerts/pod-restart-loop.json
```

预期：

- 读取 mock Kubernetes events。
- 诊断 OOMKilled 或 startup regression。
- `restart_pod` 为 L2，等待审批。

## 演示审批恢复

查询待审批：

```bash
curl "http://localhost:8000/api/approvals?status=waiting"
```

审批 L2：

```bash
curl -X POST http://localhost:8000/api/approvals/<approval_id>/approve \
  -H "Content-Type: application/json" \
  -H "X-Request-Id: req_approve_l2" \
  -d '{"approver":"demo","comment":"approved"}'
```

审批 L3：

```bash
curl -X POST http://localhost:8000/api/approvals/<approval_id>/approve \
  -H "Content-Type: application/json" \
  -H "X-Request-Id: req_approve_l3" \
  -d '{
    "approver":"demo",
    "comment":"approved with second confirmation",
    "risk_ack":true,
    "confirm_action_type":"rollback_release",
    "confirm_target":"checkout"
  }'
```

## 查看报告

```bash
curl http://localhost:8000/api/incidents/<incident_id>/report
```

重新生成报告：

```bash
curl -X POST http://localhost:8000/api/incidents/<incident_id>/report/regenerate \
  -H "X-Request-Id: req_regenerate_report"
```

预期新版本 `version = previous + 1`。
