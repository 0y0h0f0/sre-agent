# 项目范围与边界

## MVP 范围内

- 单租户本地 demo 系统。
- 1 个 demo-service。
- 4 类固定故障：数据库连接池耗尽、发布后 5xx、Redis 缓存雪崩、Pod 异常重启。
- 报警来源：Alertmanager 格式和 mock alert。
- 指标来源：Prometheus。
- 日志来源：Loki。
- Trace 来源：OpenTelemetry demo 数据或固定 mock 数据。
- Runbook 来源：本地 Markdown 文件。
- 向量检索：pgvector。
- Agent 编排：LangGraph。
- 异步任务：Celery。
- UI：React + Vite。
- 动作执行：mock executor。
- 测试：FakeLLM + fixture。

## MVP 范围外

- 不做多租户。
- 不做 RBAC、SSO、企业权限系统。
- 不操作真实云资源。
- 不对真实生产 Kubernetes 做写操作。
- 不删除数据、不修改数据库、不清空缓存。
- 不承诺诊断任意未知故障。
- 不做模型微调。
- 不做复杂通知系统。

## 风险动作边界

| 等级 | 示例 | 策略 |
| --- | --- | --- |
| L0 | 查指标、查日志、查 trace | 自动执行 |
| L1 | 创建工单、生成报告 | 自动执行 |
| L2 | 重启单个实例、扩容副本 | 人工审批 |
| L3 | 回滚版本、修改限流策略 | 人工审批 + 二次确认字段：`risk_ack=true`、`confirm_action_type`、`confirm_target` |
| L4 | 删除数据、修改数据库、清空缓存 | 直接拒绝 |

## 代码边界

- `apps/api` 不包含 LangGraph 业务节点。
- `apps/worker` 不包含 HTTP router。
- `packages/tools` 不直接操作数据库，只返回结构化结果；审计由 service 层记录。
- `packages/agent` 不直接构造 SQL，使用 repository/service。
- `packages/memory` 不直接实例化或调用 LLM provider，只负责预算、缓存 key、压缩计划、确定性压缩、schema 和记忆读写；需要 LLM 摘要时由 `packages/agent` 通过注入的 summarizer adapter 调用，并把结果写回 memory。
- `packages/rag` 只处理文档和检索，不生成处置动作。

## 非功能边界

- demo 诊断 P95 小于等于 60 秒。
- API 入队接口 P95 小于等于 300 ms。
- Runbook 检索 P95 小于等于 2 秒。
- 后端和前端覆盖率必须大于 80%。
- guardrail 覆盖率目标大于等于 95%。
