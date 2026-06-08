# 系统架构

## 主链路

```text
Alertmanager / Mock Alert
        |
        v
FastAPI POST /api/alerts
        |
        v
PostgreSQL: incidents + agent_runs
        |
        v
Celery: run_incident_diagnosis
        |
        v
LangGraph workflow
        |
        +--> MetricsTool -> Prometheus
        +--> LogsTool    -> Loki
        +--> TraceTool   -> fixture / Jaeger / Tempo read backend
        +--> GitTool     -> fixture / GitHub / Argo CD read backend
        +--> K8sTool     -> fixture / read-only live diagnostics
        +--> DbTool      -> fixture / read-only SQL diagnostics
        +--> RAG         -> runbook chunks + pgvector + BM25
        +--> Memory      -> run-local / incident / service / procedural memory
        |
        v
Diagnosis + Evidence + Actions
        |
        +--> L0/L1 auto allowed
        +--> L2/L3 human approval
        +--> L4 direct reject
        |
        v
Mock execution + Incident report + UI + Eval metrics
```

## 模块边界

| 模块 | 路径 | 责任 |
| --- | --- | --- |
| API | `apps/api` | HTTP 路由、请求校验、错误响应、鉴权、WebSocket |
| Worker | `apps/worker` | Celery app、诊断任务、审批恢复、邮件任务、周期任务 |
| Agent | `packages/agent` | LangGraph 图、节点、状态、LLM 适配、guardrail |
| Tools | `packages/tools` | 工具 query/result schema、缓存、HTTP/fixture 后端、mock executor |
| RAG | `packages/rag` | Runbook 切分、embedding（Fake/BGE-ZH/text2vec）、混合检索、rerank、草稿生成 |
| Memory | `packages/memory` | token 预算、上下文构建、压缩、记忆存储 |
| DB | `packages/db` | SQLAlchemy models、session、repositories |
| Common | `packages/common` | 配置、ID、时间、错误、Prometheus metrics |
| Evals | `packages/evals` | smoke/full/shadow eval 数据集与运行器 |
| Web | `apps/web` | React 控制台、审批 UI、报告页、E2E |
| Demo | `demo` | alert fixture、fault fixture、runbooks、demo service、topology |
| Deploy | `deploy` | Prometheus、Loki、Promtail、Grafana、OTel、BGE-ZH 配置 |

## 核心设计决定

- API 只负责创建记录和入队，不直接运行 LangGraph。
- Celery task 使用幂等逻辑处理重复投递和 worker 丢失。
- LangGraph 节点通过 `AgentDeps` 注入依赖，不直接创建数据库 session。
- PostgreSQL checkpointer 用于审批中断和恢复；真实数据库下 checkpointer 初始化失败会 fail closed。
- `agent_runs.state` 只是调试快照，不是 checkpoint 的替代品。
- 原始大量日志不直接进入 LLM prompt，必须通过 token 预算和压缩策略。
- 诊断输出必须引用 evidence ID 或 Runbook chunk ID。
- Guardrail 是确定性规则，不信任模型决定最终执行权限。
- MVP 动作执行只使用 mock executor。
- 证据交叉验证在 `diagnose` 节点融合 metrics/logs/traces/deployment 信号（权重 Trace > Metrics > Logs > Git）；corroboration 提高根因置信度，冲突设置 `_needs_human_review`。
- 级联故障分析基于服务依赖图（`SERVICE_TOPOLOGY_PATH` 配置或 trace 推导）识别故障传播链和根服务，关联同时发生的相关事故。
- LLM reasoning 可在 `diagnose` 节点启用（`LLM_REASONING_ENABLED`），输出 `diagnosis_rationale` 和 LLM 调用元数据，不持久化原始 chain-of-thought。

## 数据流

1. `POST /api/alerts` 校验告警，按 open fingerprint 去重。
2. Alert service 创建或复用 incident，并创建 agent run。
3. API 调用 `enqueue_diagnosis_task` 发送 Celery 任务。
4. Worker 锁定 agent run，标记 `running`，构造 tools、RAG、memory、LLM 和 node tracer。
5. Worker 构造 LangGraph checkpointer，启动 `AgentRunner.run()`。
6. 每个节点更新 `IncidentState`，同时持久化节点轨迹和工具调用。
7. Guardrail 根据推荐动作生成风险决策。
8. L2/L3 进入 human approval 中断；L4 直接拒绝并进入报告。
9. 审批完成后，API 更新 approval/action，再入队 `resume_incident_after_approval`。
10. Worker 使用同一 `thread_id=agent_run_id` 恢复 graph。
11. 结束后生成 incident report、同步 incident root cause、发送通知。

## 运行时依赖

| 依赖 | 用途 | 默认端口 |
| --- | --- | --- |
| PostgreSQL + pgvector | 业务表、Runbook embedding、LangGraph checkpoint | `5433:5432` |
| Redis | Celery broker/result backend、短期缓存、WebSocket 事件发布 | `6378:6379` |
| Prometheus | 指标采集 | `9090` |
| Loki | 日志查询 | `3100` |
| OTel Collector | trace demo 数据入口 | `4317`、`4318` |
| Grafana | 观测展示 | `3000` |
| API | FastAPI | `8000` |
| Web | Vite dev server | `5173` |
| Demo service | 故障注入 demo service | `8080` |
| BGE-ZH | Embedding 推理服务（BAAI/bge-small-zh, 512-dim） | `8083` |
| Mailpit | 本地 SMTP 测试（dev profile） | `8025`（Web UI）、`1025`（SMTP） |
| Celery beat | 周期任务调度（每日摘要、自动审批） | — |

## 可靠性策略

- Celery 配置 `task_acks_late=True`、`task_reject_on_worker_lost=True`、`worker_prefetch_multiplier=1`。
- Agent run row 使用数据库锁防止重复 worker 并发运行。
- `task_orphan_timeout_seconds` 后可重新执行卡住的 running run。
- Checkpointer 在真实数据库场景必须可用，否则不绕过审批。
- 工具层使用 timeout、degraded result 和缓存，避免单个外部依赖阻断整个流程。
- 邮件发送任务独立排队，可重试并记录 `email_log`。
