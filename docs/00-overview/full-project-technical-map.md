# 全项目技术地图

**最后更新：** 2026-06-23

本文从全项目视角说明各目录、进程、共享库、数据对象和测试资产如何协作。它补充 [仓库地图](repository-map.md) 和 [系统架构](architecture.md)：仓库地图回答“文件在哪里”，系统架构回答“主链路是什么”，本文回答“模块之间的技术契约是什么”。

## 一句话模型

项目由三个应用入口和八组共享库组成：

```text
apps/api      FastAPI 控制面和读 API
apps/worker   Celery + LangGraph 执行面
apps/web      React 运维控制台

packages/common      配置、错误、ID、时间、指标、脱敏和 URL 安全
packages/db          ORM 模型、session、repository
packages/agent       LangGraph 图、节点、LLM adapter、guardrail
packages/tools       诊断工具和 executor backend
packages/rag         runbook ingest/search/draft/diff
packages/memory      context budget、压缩、memory store
packages/discovery   后端发现、配置 proposal/publish/merge
packages/evals       deterministic eval runner 和 datasets
```

核心依赖方向是：

```text
apps/* -> packages/*
packages/agent -> tools + rag + memory + db + common
packages/tools -> common
packages/rag -> db + common
packages/memory -> db + rag embedding factory + common
packages/discovery -> db + common
packages/db -> common time/base only
```

`packages/common` 和 `packages/db` 是底层支撑；`apps/api` 和 `apps/worker` 是编排层；`packages/agent` 是诊断工作流核心。不要让底层包反向依赖应用层，也不要让 Agent node 直接创建外部 client 或 DB session。

下图概括应用入口、共享库和本地基础设施之间的依赖方向。详细目录职责见后续各节。

<p>
  <img src="assets/project-dependency-map.png" alt="应用与共享库依赖方向" width="900" />
</p>

## 运行面划分

| 运行面 | 代码入口 | 运行职责 | 不应承担 |
|--------|----------|----------|----------|
| API 控制面 | `apps/api/main.py` | HTTP/WebSocket、认证、schema、service 编排、DB 事务、Celery 入队 | 不运行 LangGraph，不执行真实 remediation |
| Worker 执行面 | `apps/worker/tasks.py` | Celery task、依赖构造、LangGraph run/resume、node/tool audit、报告和状态同步 | 不读取未发布 discovery proposal，不绕过 checkpoint |
| 前端展示面 | `apps/web/src/App.tsx`、`apps/web/src/api.ts` | 事件、run、审批、报告、评论、审计展示和交互；通过 TanStack Query、轮询和 WebSocket ticket 刷新实时状态 | 不决定风险等级，不恢复 graph |
| 共享业务库 | `packages/*` | 可测试的业务能力、工具、RAG、memory、发现、评测 | 不耦合 HTTP request 或 React state |
| 本地基础设施 | `docker-compose.yml`、`deploy/` | PostgreSQL/pgvector、Redis、Prometheus、Loki、Grafana、demo service | 不默认启用真实写路径 |

## 项目级数据对象

本节说明对象所有权。SQLAlchemy 类型、Alembic revision 链、repository 事务边界、checkpoint pointer、pgvector fallback 和 append-only/audit 细节见 [数据模型、迁移与持久化技术深挖](data-model-migrations-persistence-deep-dive.md)。

| 对象 | 主模型/表 | 主要写入者 | 主要读取者 | 技术要点 |
|------|-----------|------------|------------|----------|
| Incident | `Incident` / `incidents` | `AlertService`、`IncidentService`、worker 状态同步 | incident API、frontend、report service | `fingerprint` 去重 open incident；root cause summary 是展示字段 |
| AgentRun | `AgentRun` / `agent_runs` | alert/manual diagnose、worker | Agent run API、frontend、eval/report | `state` 是展示快照；checkpoint 依赖 LangGraph saver |
| Node trace | `AgentRunNode` / `agent_run_nodes` | worker `node_tracer` | Agent run API、WebSocket/front-end | 每个节点记录 status、duration、input/output summary、error |
| Tool call | `ToolCall` / `tool_calls` | worker `tool_call_recorder` | Agent run API、tool audit tests | 记录 query/result/cache hit/cache key，供排障和缓存指标使用 |
| Evidence | `EvidenceItem` / `evidence_items` | Agent evidence 节点 | diagnosis/report/API/frontend | state evidence 回填 `evidence_id` 后才能被安全引用 |
| Action | `Action` / `actions` | `human_approval`、`execute_action`、service | approval/action API、frontend、report | 风险等级来自 guardrail，不来自前端 |
| Approval | `Approval` / `approvals` | `human_approval`、approval service | approval API、resume node | L3 保存 `risk_ack`、`confirm_action_type`、`confirm_target` |
| Report | `IncidentReport` / `incident_reports` | `generate_report`、report regenerate API | report API、frontend | `(incident_id, version)` 唯一；重新生成创建新版本；生命周期细节见 `report-generation-incident-lifecycle-deep-dive.md` |
| Runbook chunk | `RunbookChunk` / `runbook_chunks` | runbook ingest/review | retriever、RunbookSearchTool | primary embedding 当前是 512 维 |
| Runbook draft | `RunbookDraft` / `runbook_drafts` | cluster/template/LLM draft service | runbook API、review flow | 草稿不等于已发布知识；M9 LLM draft 为 `pending_review` |
| Runbook version | `RunbookVersion` / `runbook_versions` | draft publish flow | version API、RAG/debug | `(document_id, version_number)` 唯一；publish 后才进入 version history |
| Amendment draft | `AmendmentDraft` / `amendment_drafts` | incident diff service、future feedback wiring | amendment API、audit/debug | review/apply 只更新 lifecycle metadata，不自动合并或发布 runbook |
| Memory item | `MemoryItem` / `memory_items` | `compress_context`、`persist_memory` | `retrieve_memory` | L0-L3 scope；embedding nullable 512 维 |
| Effective config | `EffectiveConfigVersion` | config/discovery publish | worker `_build_deps()` | worker 只读 published config |
| Eval run | `EvalRun` | eval API/task/shadow | eval API | CI smoke 使用 FakeLLM，真实 provider 不做稳定门禁 |

## 模块契约

### API contract

API 层固定使用 `router -> service -> repository -> model`：

- Router 处理 path/query/body、依赖注入和 response model。
- Service 处理业务规则、事务、冲突校验、审计和 task enqueue。
- Repository 封装 SQLAlchemy 查询、状态转换和 `SELECT ... FOR UPDATE`。
- Schema 与 ORM model 分离。

跨 API 的统一约束：

- 所有写 API 应支持 `X-Request-Id`。
- 标准业务错误使用 `{ "error": { "code", "message", "request_id", "details" } }`。
- API key auth 和 scope enforcement 位于 middleware/dependency，不写在业务 service 内。
- `POST /api/alerts` 和 `POST /api/incidents/{incident_id}/diagnose` 只入队诊断任务。

更完整的 middleware、request id、错误信封、scope、rate limit、service 事务、Celery 入队和审计路径见 [API 控制面与服务层技术深挖](api-control-plane-service-deep-dive.md)。

### Alert source contract

告警入口最终都归一到 `AlertService.create_alert()`：

- `/api/alerts` 支持统一 payload，也能通过 `AlertCreateRequest.normalize_provider_payload()` 识别 Alertmanager、PagerDuty、Grafana、Datadog 和 custom 形状。
- Alertmanager webhook 和 poll 都使用 `source=alertmanager`，通过 fingerprint 与 open incident 去重。
- Alertmanager poll 由 Beat task 触发，必须有 bounded scope；`filter_hash` 同时用于 Redis lock、cursor namespace 和 poll audit。
- `AlertPollCursor` 用 `(filter_hash, fingerprint)` 记录 `first_seen_at`、`last_seen_at` 和 `missing_rounds`，resolved inference 是 active alert 缺失的保守推断。
- Grafana-shaped payload 当前已通过 `/api/alerts` 接线；`AlertService.ingest_grafana_alert()` 是 gated helper，但没有独立注册的 Grafana webhook router，`GRAFANA_WEBHOOK_SECRET_REF` / `GRAFANA_WEBHOOK_MAX_BYTES` 也未接到公开 route。

更完整的 parser、fingerprint、NFA suppression、poll scope、cursor、resolved inference 和 Grafana 当前接线边界见 [Alertmanager Poll、Grafana 与告警来源归一化技术深挖](alert-source-normalization-poll-grafana-deep-dive.md)。

### Security control contract

认证、授权和审计固定在 API 控制面入口处处理，业务 service 只消费已建立的身份和 request id：

- `apps/api/main.py` 先写入 `X-Request-Id`，再运行 API key middleware。
- `apps/api/middleware/auth.py` 负责 Bearer key、开放路径、bootstrap seed、hash 校验和 `last_used_at`。
- `apps/api/dependencies.py` 的 `require_scope()` 是任一 scope 语义；需要多个 scope 同时具备时应写自定义 dependency。
- `API_KEY_AUTH_ENABLED=false` 时 auth 和 scope 都跳过，只能用于本地 demo/测试，不是生产口径。
- WebSocket 使用短期 incident-scoped ticket，不把长期 API key 放到 WebSocket URL。
- `AuditLogRepository` 只提供 create/query；raw secret、Authorization header 和 provider token 不应进入 audit details。
- backend secret 使用 `env:VAR_NAME` 引用，runtime-only 解析；prompt/audit/external context 先经过 redaction。

更完整的 API key、scope、bootstrap、WebSocket ticket、rate limit、audit 和 redaction 路径见 [认证、API Key、审计与安全边界技术深挖](auth-api-key-audit-security-deep-dive.md)。

### Operator interaction contract

操作员交互面由邮件、实时事件、浏览器通知、评论/标注和 audit 共同组成：

- 业务状态必须先持久化，再触发 email/WebSocket/browser notification。
- 邮件通知先写 `EmailLog`，再由 Celery `send_email_notification` 发送；通知失败不阻断诊断主链路。
- L2 email token 可以批准/拒绝；L3 不能通过 email token 批准，必须回到 Web 控制台完成二次确认。
- WebSocket 事件只触发前端 query invalidation，不是状态 source of truth。
- 浏览器通知由前端根据新出现的 waiting approval 本地触发，不是服务端 push subscription。
- Incident comments 和 evidence annotations 写入业务表，并在创建时写 audit。

更完整的 email queue/log、approval email token、comments、evidence annotations、WebSocket/service worker 通知和操作员审计串联见 [通知、邮件、评论协作与操作员交互技术深挖](notifications-collaboration-operator-interaction-deep-dive.md)。

### Feedback and learning contract

反馈与持续学习路径是审计化的数据回流面，不是自动训练或自动执行面：

- NFA mark 写 `FalsePositivePattern`、`FeedbackItem` 和 audit；达到阈值后将当前/后续相同 fingerprint incident 降级为 `P4`，不会丢弃告警。
- 根因修正会更新 `incidents.root_cause_summary` 并写 feedback/audit；不会自动重跑 Agent、重写旧报告或修改 checkpoint。
- Action feedback 只写 `feedback_items` 和 audit；不会新增、删除或执行 `actions` 表中的 action。
- 相关事件 API 当前按相同 fingerprint 和相同 service 查询；不会自动执行 vector similarity，也不读取已持久化 pair correlation。
- `RunbookFeedbackAnalyzer` 是 deterministic library 能力；当前没有自动 worker/API 编排落库 summary 或 amendment。
- M9 incident diff 只能创建 `AmendmentDraft(status=pending_review)`，review/apply 只更新 amendment lifecycle metadata，不自动发布 runbook。
- API feedback route 当前没有注入 `MemoryStore`，所以 feedback 默认不进入 memory；eval 也不会自动从 feedback 生成 case。

更完整的 NFA、feedback、correlation、runbook feedback analyzer、amendment draft 和 memory/eval 回流边界见 [反馈、NFA、关联事件与持续学习技术深挖](feedback-nfa-correlation-continuous-learning-deep-dive.md)。

### Data persistence contract

持久化层固定使用 `model -> migration -> repository -> service/worker` 的顺序演进：

- `packages/db/models.py` 是 ORM 结构入口，Alembic 迁移是数据库 schema 来源；新增字段不能只改其中一边。
- PostgreSQL 使用 JSONB、pgvector 和 TSVECTOR；SQLite/test fallback 使用 JSON/Text，保证单元测试不依赖外部 PostgreSQL 特性。
- Public ID 面向 API 和日志，整数主键只做内部 join。
- `agent_runs.state` 是展示快照，LangGraph 恢复依赖 PostgresSaver，业务表只保存 checkpoint pointer。
- repository 封装锁、版本号、查询和状态变更；常规事务由 service/worker 提交，`PollCursorRepository` 是当前少数内部 commit 的例外。
- 报告、runbook version、EffectiveConfig 等版本化对象只追加新版本，不覆盖历史版本。

新增数据模型、迁移或持久化不变量时，先看 [数据模型、迁移与持久化技术深挖](data-model-migrations-persistence-deep-dive.md)，再同步 [数据模型](../01-backend/data-model.md) 和 [状态与 ID](../11-reference/status-and-ids.md)。

### Report lifecycle contract

报告是 append-only 的 incident 内版本序列，不是覆盖式文档：

- `generate_report` 节点读取 root cause、actions、采集 evidence、verify evidence、runbook evidence、verify gates 和 review flag，调用 `IncidentReportRepository.next_version()` / `create()`。
- `POST /api/incidents/{incident_id}/report/regenerate` 不重跑 LangGraph；它读取 latest run state、当前 evidence/actions 和 incident 展示字段，创建新 report version。
- `GET /api/incidents/{incident_id}/report` 只返回 latest report；当前没有按历史 version 读取的公开 endpoint。
- `incident.root_cause_summary` 是列表/详情展示摘要，不替代报告历史。
- `agent_runs.state` 是展示和 report regeneration 输入之一，不替代 checkpoint。
- worker 成功终态有 `execution_results` 时将 incident 标为 `mitigated`，否则标为 `resolved`；waiting approval 主要由 run status 表达。
- report notification 在报告持久化后入队，通知失败不应回滚已创建报告。

完整路径见 [报告生成、版本与事件生命周期技术深挖](report-generation-incident-lifecycle-deep-dive.md)。

### Worker contract

Worker 是所有诊断执行的统一入口。`run_incident_diagnosis_logic()` 的固定步骤是：

```text
lock AgentRun
-> idempotency/orphan check
-> mark running and commit
-> build AgentDeps
-> build PostgresSaver checkpointer
-> AgentRunner.run()
-> waiting_approval / failed / succeeded status sync
```

至少一次投递由 `AgentRunRepository.get_for_update()` 和 run status 判断处理。真实 PostgreSQL 下 checkpointer 初始化失败必须 fail closed，不能退回到无 checkpoint 的自动审批路径。

更完整的 task 幂等、orphan recovery、`PostgresSaver` 初始化、`GraphInterrupt` resume、node/tool audit、通知、poll/discovery/eval task 执行边界见 [Worker、Celery 与 LangGraph Checkpoint 技术深挖](worker-celery-langgraph-checkpoint-deep-dive.md)。

### Agent contract

`packages/agent` 的节点都是普通 Python 函数，形状是：

```python
def node(state: IncidentState, deps: AgentDeps) -> IncidentState:
    ...
```

节点边界：

- 依赖通过 `AgentDeps` 注入。
- 节点不直接创建 DB session、HTTP client、Kubernetes client 或 LLM provider。
- 节点要写 node trace，工具调用要通过 recorder 记录。
- 大日志不直接进 state/prompt，必须经过 context builder/compressor。
- 执行类动作必须经过 `guardrail_check`，不能从 planner 或 runbook 直接执行。

LangGraph checkpoint config 固定为：

```python
{"configurable": {"thread_id": agent_run_id, "checkpoint_ns": ""}}
```

### Execution action contract

执行类动作必须同时满足风险、审批、能力、快照、executor 和验证六个条件：

- `guardrail_check` 决定 L0-L4、`allowed` 和 `requires_approval`。
- L2/L3 必须由 `human_approval` 创建 Action/Approval，并在 API 决策提交后 resume。
- L3 approve 必须保存 `risk_ack`、`confirm_action_type` 和 `confirm_target`。
- `packages/agent/actions/capabilities.py` 声明 live backend、snapshot contract、preflight checks 和 verify gates。
- `take_snapshot`、`execute_action` 和 `verify` 使用同一个 effective executor namespace。
- `EXECUTOR_BACKEND=fixture` 是默认路径；`EXECUTOR_BACKEND=live` 只允许当前受控 Kubernetes restart/pause/resume/scale/rollback mutation。
- `/api/actions/{action_id}/execute` 当前固定使用 fixture executor，不是 live Kubernetes 写入口。

更完整的 fixture/live executor、capability metadata、pre-action snapshot、execute preflight、verify/replan 和手动 action execute API 边界见 [执行器、动作能力与验证闭环技术深挖](executor-action-verification-loop-deep-dive.md)。

### Tool contract

普通诊断工具遵守同步 `BaseTool.run(query) -> ToolResult` 协议：

- query 是 Pydantic schema。
- result 是 `ToolResult`，包含 status、data、summary、evidence、cache metadata 和 error。
- 外部调用必须有 timeout。
- 失败返回 degraded/timeout/failed，不让 worker 因后端缺失崩溃。
- cache key 必须稳定，包含必要的 backend/datasource 维度。

写入能力不属于普通 diagnostics tool。真实 remediation 只能通过 executor backend，并且必须经过 guardrail、approval、snapshot、execute、verify。

### RAG contract

Runbook RAG 提供知识检索，不提供执行许可：

- ingest 将 Markdown 拆成 chunk，写 `runbook_chunks`。
- retriever 返回 `chunk_id`、`source_path`、`title`、`excerpt`、`score`、`metadata`。
- diagnosis/report 引用 runbook 时必须保留 chunk ID 或 evidence ID。
- LLM draft 和 amendment draft 只能是 `pending_review`，不会自动发布。
- external embedding/web search/semantic search 均属于 M9 gated path，生产默认关闭。

### Runbook lifecycle contract

Runbook 内容的生命周期分三层，不能混用：

- `RunbookChunk` 是可检索知识；由 Markdown ingest 或 draft publish 后的 chunk ingest 写入。
- `RunbookDraft` 是待审内容；incident-cluster、template、regenerate 和 M9 LLM draft 都不会自动进入 RAG。
- `RunbookVersion` 是发布历史；当前 draft publish 使用 `draft_id` 作为 `document_id` 并创建递增版本。
- `AmendmentDraft` 是修改建议；M9 incident diff 创建 `pending_review` amendment，review/apply 只写状态和目标引用。
- `applied` 不代表系统自动修改了 draft/version 内容，也不会自动重新 ingest chunks。

完整路径见 [Runbook 草稿、版本与 Amendment 生命周期技术深挖](runbook-draft-version-amendment-lifecycle-deep-dive.md)。

### Memory and context contract

`packages/memory` 负责预算和确定性压缩，不直接调用 LLM：

- `ContextBuilder` 组装 prompt messages 和 token usage estimate。
- `Compressor` 压缩超预算 evidence，并保留 retained/omitted evidence ID。
- `MemoryStore` 读写 L0 run、L1 incident、L2 service、L3 global procedural memory。
- provider prompt cache、tool request-local cache、app prompt segment cache 是三种不同指标，不能混用。

### Frontend contract

前端只通过 `apps/web/src/api.ts` 调 API：

- 每个请求带 `X-Request-Id` 和可选 bearer API key。
- 标准错误信封转成 `ApiError`。
- 页面用 TanStack Query 管理 server state，mutation 成功后 invalidate 相关 query。
- Agent Run 页面使用 5 秒轮询和 WebSocket 事件共同刷新。
- L3 二次确认字段由 UI 采集，但最终校验仍在后端。

更完整的 query key、mutation invalidation、WebSocket ticket、Redis Pub/Sub 和通知链路见 [前端控制台与实时更新技术深挖](frontend-realtime-console-deep-dive.md)。

### Discovery and config contract

Discovery 负责发现和提议，不负责擅自修改 worker 运行配置：

- detection/proposal 可以失败降级，不阻塞 agent 启动。
- production discovery 不能自动发布配置。
- worker 只读取 latest published `EffectiveConfigVersion`。
- 配置合并优先级是 `env > active override > profile > published > safe default`。
- 后端 URL 必须通过 URL safety 校验，生产环境拒绝 localhost、metadata endpoint、link-local 和私网危险目标，除非显式 allowlist。

更完整的 discovery runner、K8s/Prometheus/Loki/Jaeger 发现、backend endpoint detection、capability matrix、workload binding、service edge、manual/auto rerun 和 proposal 边界见 [Discovery、Capability Matrix 与服务拓扑技术深挖](discovery-capability-topology-deep-dive.md)。

## 安全边界如何贯穿全项目

| 边界 | API | Worker/Agent | Tools/Executor | Frontend/Docs |
|------|-----|--------------|----------------|---------------|
| 默认 fixture executor | API 不直接执行真实动作 | `_build_deps()` 默认 fixture | live executor 需要 `EXECUTOR_BACKEND=live` | UI 不提供绕过路径 |
| L2/L3 审批 | approval service 写决策并入队 resume | `human_approval` interrupt/resume | executor 只执行已允许动作 | UI 采集 L3 字段 |
| L4 拒绝 | 不创建可执行审批路径 | `guardrail_check` 标记直接报告 | executor 不支持 destructive 动作 | 文档不得写成可配置开启 |
| 只读诊断 | API 暴露诊断结果 | collect/verify 只读查询 | K8s/DB diagnostics 限定 read-only | 页面只展示 evidence |
| Secret 不外泄 | middleware/service 不落原始 key | prompt/state 元数据脱敏 | external call 前 redaction | 文档使用 secret ref，不写 raw secret |
| M9 default-off | feature gates 控制 API path | worker 保持 M0-M8 确定性路径 | external LLM/web/embedding gated | docs 标明生产默认关闭 |

## 配置影响范围

| 配置类别 | 主要字段 | 影响模块 | 常见误区 |
|----------|----------|----------|----------|
| Core service | `DATABASE_URL`、`REDIS_URL`、Celery URLs | API、worker、DB、Redis | Compose 宿主机端口和容器内服务名不同 |
| Observability | `PROMETHEUS_URL`、`LOKI_URL`、`TRACE_BACKEND` | tools、worker deps、discovery | 单实例当前只有一套 active backend，不是多后端 fan-out；后端适配器边界见 `observability-backend-adapters-deep-dive.md` |
| Deployment changes | `DEPLOYMENT_BACKEND`、`GITHUB_*`、`ARGOCD_*` | GitChangeTool、deployment backend、collect_deployment | GitHub/Argo CD 是只读变更证据来源，不是发布或 rollback 执行入口；细节见 `deployment-change-github-argocd-deep-dive.md` |
| Executor | `EXECUTOR_BACKEND`、`EXECUTOR_K8S_NAMESPACE` | worker、executor backend、capability/preflight/verify path | production 不会自动把 live 改回 fixture；live allowlist 与风险表不是一回事，细节见 `executor-action-verification-loop-deep-dive.md` |
| RAG/embedding | `EMBEDDING_PROVIDER`、`RUNBOOK_HYBRID_SEARCH_ENABLED` | runbook ingest/search、memory search | primary vector store 是 512 维 |
| LLM | `LLM_PROVIDER`、`LLM_REASONING_ENABLED`、profile/latency flags | diagnose/report/runbook draft/diff | 真实 LLM 不能做 CI 稳定门禁；provider cache 三态、compact diagnosis、deterministic report 和 parallel specialist 边界见 `llm-prompt-fakellm-provider-boundaries-deep-dive.md` |
| M9 | `M9_EXTENSIONS_ENABLED` 和子开关 | RAG、trace、Grafana parser/helper、semantic search | global gate false 会强制关闭 M9 子能力；Grafana helper 当前未暴露为独立 router |
| Auth | `API_KEY_AUTH_ENABLED`、bootstrap/admin scope | API middleware、frontend API key panel | auth disabled 时 scope dependency 会跳过 |
| Notifications | `SMTP_*`、`SRE_EMAIL_LIST`、`WEB_BASE_URL`、`NOTIFICATION_TIMEZONE` | email service、worker tasks、templates、frontend links | 邮件不是诊断硬依赖；配置缺失会 skipped |
| Feedback/learning | `NFA_AUTO_SUPPRESS_THRESHOLD`、`NFA_RESET_DAYS`、`CROSS_INCIDENT_MAX_RESULTS`、`RUNBOOK_AMENDMENT_*` | feedback service、alert service、runbook feedback analyzer | NFA 当前降级不丢弃；runbook feedback analyzer 当前未自动接线 |
| Poll/discovery | `ALERT_SOURCE`、`DISCOVERY_ENABLED`、`K8S_BACKEND` | beat、worker tasks、discovery | Alertmanager poll 需要 bounded scope/filter hash/cursor；production discovery 默认关闭；manual rerun scope/lock 入队；proposal 不等于 published；细节见 `alert-source-normalization-poll-grafana-deep-dive.md` 和 `discovery-capability-topology-deep-dive.md` |

## 变更落点

| 要改什么 | 应先看 | 代码落点 | 文档落点 | 测试落点 |
|----------|--------|----------|----------|----------|
| 新 API endpoint | API 参考、后端架构、API 控制面深挖 | router、schema、service、repository | `docs/01-backend/*`、`api-control-plane-service-deep-dive.md` | integration + schema/API client tests |
| 新认证、scope、审计或 secret redaction 行为 | 认证/API Key/审计深挖、认证与 API 密钥 | `apps/api/middleware/auth.py`、`apps/api/dependencies.py`、`apps/api/routers/api_keys.py`、`apps/api/services/ws_ticket_service.py`、`packages/db/repositories/audit_logs.py`、`packages/common/redaction.py` | `auth-and-api-keys.md`、`auth-api-key-audit-security-deep-dive.md`、`configuration.md` | auth/scope/audit/redaction integration tests |
| 新通知、email token、评论或操作员交互 | 通知/协作/操作员交互深挖、Celery、前端控制台 | `apps/api/services/email_service.py`、`apps/worker/tasks.py`、`apps/api/routers/approvals.py`、`apps/api/routers/comments.py`、`apps/api/ws/`、`apps/web/src/App.tsx`、`apps/web/public/sw.js` | `notifications-collaboration-operator-interaction-deep-dive.md`、`celery-and-jobs.md`、`react-console.md`、`api-reference.md` | email/unit + phase6 integration + frontend tests |
| 新告警来源、Alertmanager poll 或 Grafana webhook 行为 | 告警来源归一化深挖、API 参考、Celery、配置参考 | `apps/api/schemas/alerts.py`、`apps/api/services/alert_service.py`、`apps/api/routers/alerts.py`、`apps/worker/tasks.py`、`packages/discovery/matcher_parser.py`、`packages/db/repositories/poll_cursor.py` | `alert-source-normalization-poll-grafana-deep-dive.md`、`api-reference.md`、`celery-and-jobs.md`、`configuration.md`、必要时 M9 文档 | alert API + poll cursor/resolved + parser/fingerprint tests |
| 新 NFA、feedback、correlation 或学习回流能力 | 反馈/NFA/持续学习深挖、API 参考、Runbook RAG、Memory、Eval | `apps/api/services/feedback_service.py`、`packages/db/repositories/feedback.py`、`packages/db/repositories/false_positive_patterns.py`、`packages/db/repositories/incident_correlations.py`、`packages/discovery/runbook_feedback.py`、`apps/api/services/runbook_service.py` | `feedback-nfa-correlation-continuous-learning-deep-dive.md`、`api-reference.md`、`runbook-rag.md`、`runbook-draft-version-amendment-lifecycle-deep-dive.md`、`memory-cache-compression.md`、`evaluation.md` | feedback unit/integration + runbook feedback + amendment review + memory/eval tests when wired |
| 新 DB 表、字段、迁移或 repository 不变量 | 数据模型、数据模型/迁移/持久化深挖 | `packages/db/models.py`、`migrations/versions/`、`packages/db/repositories/`、相关 service/worker | `data-model.md`、`status-and-ids.md`、`data-model-migrations-persistence-deep-dive.md` | migration/repository/integration + safety tests |
| 修改 Worker/Celery/checkpoint 行为 | Celery 与异步任务、Worker 执行面深挖 | `apps/worker/tasks.py`、`apps/worker/celery_app.py`、`packages/agent/runner.py` | `celery-and-jobs.md`、`worker-celery-langgraph-checkpoint-deep-dive.md`、必要时 `workflow.md` | worker integration + graph resume/checkpoint tests |
| 新 Agent 节点 | Agent 工作流 | `graph.py`、`nodes/`、`state.py` | `docs/02-agent/workflow.md` | node unit + graph/integration |
| 新报告生成、再生成或 incident lifecycle 行为 | 报告生命周期深挖、API 参考、前端控制台 | `packages/agent/nodes/generate_report.py`、`apps/api/services/report_service.py`、`packages/db/repositories/reports.py`、`apps/worker/tasks.py`、`apps/web/src/App.tsx` | `report-generation-incident-lifecycle-deep-dive.md`、`api-reference.md`、`data-model.md`、`react-console.md` | report API integration + report node + frontend report tests |
| 新 LLM provider、prompt、FakeLLM 覆盖或 M9 LLM draft 行为 | LLM 与提示词、LLM/Prompt/FakeLLM 边界深挖、Eval、Runbook RAG | `packages/agent/llm/`、`packages/agent/prompts.py`、`packages/agent/fake_llm.py`、`packages/rag/llm_runbook_generator.py`、`packages/rag/incident_diff.py` | `llm-and-prompts.md`、`llm-prompt-fakellm-provider-boundaries-deep-dive.md`、`evaluation.md`、`runbook-rag.md` | provider/fallback/redaction + FakeLLM smoke/eval + M9 draft tests |
| Agent run LLM/Web latency 优化 | Latency plan/cards、LLM 与提示词、Memory、Runbook RAG、Worker 深挖 | `packages/agent/llm/`、`packages/agent/nodes/`、`packages/memory/`、`packages/rag/runbook_web_context.py`、`packages/common/metrics.py`、worker metrics 汇总 | `plans/12-latency/*`、`llm-and-prompts.md`、`memory-cache-compression.md`、`runbook-rag.md`、`worker-celery-langgraph-checkpoint-deep-dive.md` | `test_llm_providers.py`、`test_diagnose_multi_perspective.py`、`test_memory.py`、`test_web_search_safety.py`、worker integration |
| 新工具后端或 observability adapter | 工具层、配置参考、工具与证据深挖、Observability 后端适配器深挖 | `packages/tools/`、worker `_build_deps()`、`packages/discovery/` | `tool-layer.md`、`tool-evidence-deep-dive.md`、`observability-backend-adapters-deep-dive.md`、`configuration.md`、必要时 `discovery-capability-topology-deep-dive.md` | mocked backend + degraded/read-only/secret leakage tests |
| 新 deployment change 后端或映射规则 | Deployment Change 深挖、工具层、配置参考 | `packages/tools/git_changes.py`、`packages/tools/deployment_backends.py`、`packages/agent/nodes/collect_deployment.py`、worker `_build_deps()` | `deployment-change-github-argocd-deep-dive.md`、`tool-layer.md`、`configuration.md`、必要时 `tool-evidence-deep-dive.md` | fixture/GitHub/Argo mapping + degraded/redaction/cache datasource tests |
| 新执行动作 | guardrail/approval、executor/verify 深挖 | guardrail policy、capabilities、snapshot、executor backend、verify、action API if needed | `guardrails-and-approval.md`、`executor-action-verification-loop-deep-dive.md`、`tool-layer.md`、scope docs | guardrail/capability/executor/approval/verify/replan negative tests |
| 新 runbook/RAG 能力 | Runbook RAG、RAG/记忆/上下文深挖、Runbook 生命周期深挖 | `packages/rag/`、`apps/api/services/runbook_service.py`、runbook service/repo | `runbook-rag.md`、`rag-memory-context-deep-dive.md`、`runbook-draft-version-amendment-lifecycle-deep-dive.md` | ingest/search/draft/version/amendment contract |
| 新 memory/compression 行为 | Memory 文档、RAG/记忆/上下文深挖 | `packages/memory/`、agent build/compress nodes | `memory-cache-compression.md`、`rag-memory-context-deep-dive.md` | compression/evidence ID tests |
| 新配置、feature flag 或 EffectiveConfig 行为 | 配置参考、配置/EffectiveConfig 深挖 | `settings.py`、`feature_flags.py`、`packages/discovery/config_*`、worker deps builder | `configuration.md`、`config-discovery-effective-config-deep-dive.md` 和相关专题 | settings + production safety + worker effective config |
| 新 discovery、capability matrix 或 topology 行为 | Discovery/Capability/Topology 深挖 | `packages/discovery/`、`apps/api/routers/discovery.py`、`apps/worker/tasks.py` | `discovery-capability-topology-deep-dive.md`、`api-reference.md`、`k8s-backend-verification.md` | discovery unit + API auth/rerun integration |
| 新前端页面 | React 控制台、前端实时深挖 | `App.tsx`、`api.ts`、styles | `react-console.md`、`frontend-realtime-console-deep-dive.md` | Vitest + Playwright when needed |
| 新部署资源 | 本地演示/K8s docs | `docker-compose.yml`、`deploy/` | `local-demo.md`、deploy docs | smoke/manual verification |
| 新 eval case | 评测体系 | `packages/evals/datasets/` | `evaluation.md` | eval runner integration |
| 新测试门禁或工程指标 | 测试策略、评测体系、工程指标深挖 | `tests/`、`.github/workflows/ci.yml`、`apps/api/services/engineering_metrics_service.py` | `testing-strategy.md`、`evaluation.md`、`engineering-metrics.md`、`testing-eval-engineering-metrics-deep-dive.md` | targeted tests + metrics API coverage |
| 新生产发布、回滚或运维流程 | 生产检查清单、运维手册、M9 rollout、生产运维深挖 | `packages/common/settings.py`、`deploy/k8s/`、`docker-compose.yml`、health/metrics/config/worker 入口 | `production-checklist.md`、`final-pre-execution-checklist.md`、`operator-runbook.md`、`production-operations-rollback-deep-dive.md` | production safety + focused smoke/manual verification |

## 横向调试入口

| 问题类型 | 首看数据 | 首看代码 | 判断方向 |
|----------|----------|----------|----------|
| 告警未生成 incident | API response/request ID、API logs | `alerts.py`、`alert_service.py` | auth、rate limit、payload validation、dedup；poll/Grafana 细节见告警来源归一化深挖 |
| API 返回 401/403 | error envelope、request ID、API key metadata | `auth.py`、`dependencies.py`、route dependencies | open path、Bearer 格式、revoked/expired、缺 scope、auth disabled 差异 |
| WebSocket 连接失败 | ticket response、close code、API logs | `ws_ticket_service.py`、`apps/api/ws/router.py` | ticket secret、TTL、incident mismatch、auth disabled/enabled |
| run 不执行 | `agent_runs.status`、`celery_task_id` | `tasks.py`、Celery config | worker 是否在线、Redis broker、idempotency 状态 |
| 节点卡住或失败 | `agent_run_nodes`、worker logs | `packages/agent/nodes/*` | node trace error、tool degraded、checkpoint |
| 工具无数据 | `tool_calls`、tool output summary | `packages/tools/*`、`_build_deps()` | backend URL、effective config、cache key、fixture path；细节见 `observability-backend-adapters-deep-dive.md` |
| 审批后未继续 | `approvals.status`、`actions.status` | `approval_service.py`、`human_approval.py` | 是否仍有 waiting approval、resume 是否入队 |
| 执行动作被阻断 | `actions.status`、`execution_results`、`pre_action_snapshot` | `execute_action.py`、`capabilities.py`、`executor_backends.py` | fixture/live backend、capability、snapshot identity、params whitelist、DNS-1123 target/namespace |
| 验证后反复重规划 | `verify_result`、`verify_gates`、fresh evidence | `verify.py`、`plan_actions.py` | required gate 是否 degraded/unchanged/unknown，degraded 后是否只允许 rollback action |
| 邮件没发或重复 | `email_log`、worker logs、SMTP 配置 | `email_service.py`、`send_email_notification` | queued/sent/failed/skipped、related id 去重、SMTP 缺失 |
| Email token 失败 | `approvals.email_token`、`email_token_expires_at` | `approval_service.py`、`approvals.py` | token 是否已用/过期；L3 approve 是否被正确拒绝 |
| 评论或标注异常 | comments/annotations response、audit log | `comment_service.py`、`comments.py` | incident/evidence 是否存在、schema 长度、audit 是否写入 |
| 报告缺失或版本异常 | `incident_reports`、run state、`agent_run_nodes` | `generate_report.py`、`report_service.py`、`reports.py` | run 是否终态、report node 是否失败、latest run 是否存在、version 是否连续 |
| Runbook draft 发布后搜不到 | `runbook_drafts`、`runbook_versions`、`runbook_chunks`、API response | `runbook_service.py`、`packages/rag/*` | draft 是否 `published`、version 是否创建、chunk ingest 是否降级/跳过、`source_path` 是否为 `drafts/{draft_id}.md` |
| 前端展示旧数据 | query key、network request ID | `api.ts`、`App.tsx` | polling、WebSocket ticket、mutation invalidation |
| 配置没生效 | env、published config、override | `settings.py`、`config_merge.py`、`_build_deps()` | env 优先级、production safety default、published 状态 |
| Discovery 有结果但诊断仍 degraded | discovery run/proposal、published config、tool calls | `discovery.py`、`tasks.py`、`config_merge.py` | proposal 是否仍 pending、是否 publish、worker 是否读取 latest published config |
| Topology 或 capability matrix 缺数据 | `discovery_runs.summary`、API response | `runner.py`、`topology.py`、`capability_assessor.py` | 最近 run 是否有 summary、K8s/Jaeger/Loki/Prometheus 是否 degraded、CapabilityAssessor 是否误认为已接入 API |
| 迁移或模型不一致 | Alembic revision、表结构、ORM 字段 | `models.py`、`migrations/versions/`、repository | migration 是否缺失、维度/索引/唯一约束是否和 ORM 一致 |
| 审计或 secret 泄漏风险 | `audit_logs.details`、response body、node/tool summaries | `audit_logs.py`、`redaction.py`、service 写审计处 | 是否写入 raw key、Authorization、token、password、private key |

## 代码审查时的项目级检查

- 这个变更有没有越过 `router -> service -> repository` 或 `AgentDeps` 注入边界。
- 是否新增敏感 endpoint；如果有，auth、scope、open path、WebSocket ticket、audit 和 redaction 是否明确。
- 是否新增操作员通知或协作写路径；如果有，主业务提交、email/WebSocket/browser notification、audit 和失败降级是否分清。
- 是否新增或修改了持久化对象；如果有，ORM、migration、repository、状态/ID 文档和迁移测试是否同步。
- 是否引入了新的真实外部调用；如果有，是否具备 feature flag、timeout、redaction、audit/metric 和 degraded path。
- 是否新增或扩大真实写路径；如果有，是否仍在 executor backend、guardrail、approval 和 verify 链路内。
- 是否把风险表误当作 live executor allowlist；新增 live action 是否同步 capability、snapshot、preflight、verify 和 rollback 语义。
- 是否把 `agent_runs.state` 当成 checkpoint 或 source of truth。
- 是否把大日志、secret、token、auth header 写入 DB、audit、state 或 prompt。
- 是否保持 FakeLLM、fake embedding、fixture executor 的 CI 默认路径。
- 是否更新了对应专题文档，而不只是 README。
- 是否选择了最小但足够的测试层级。

## 展示项目技术细节的推荐讲解顺序

1. 从 [告警到报告技术深挖](alert-to-report-deep-dive.md) 讲主链路。
2. 用 [Alertmanager Poll、Grafana 与告警来源归一化技术深挖](alert-source-normalization-poll-grafana-deep-dive.md) 解释 `/api/alerts`、provider parser、poll cursor、resolved inference 和 Grafana 当前接线边界。
3. 用本文讲各模块契约和数据对象所有权。
4. 用 [API 控制面与服务层技术深挖](api-control-plane-service-deep-dive.md) 解释 FastAPI 请求如何穿过 auth、scope、service 事务、Celery 入队和审计。
5. 用 [认证、API Key、审计与安全边界技术深挖](auth-api-key-audit-security-deep-dive.md) 解释 API key、scope、WebSocket ticket、rate limit、audit 和 redaction。
6. 用 [数据模型、迁移与持久化技术深挖](data-model-migrations-persistence-deep-dive.md) 解释 ORM、migration、repository、public ID、checkpoint pointer 和版本化对象。
7. 用 [Worker、Celery 与 LangGraph Checkpoint 技术深挖](worker-celery-langgraph-checkpoint-deep-dive.md) 解释 API 入队后的执行面、run 幂等、checkpoint、approval resume 和 task 边界。
8. 展示 `agent_run_nodes`、`tool_calls`、`evidence_items`、`incident_reports` 如何把一次 run 可审计化。
9. 用 [工具与证据技术深挖](tool-evidence-deep-dive.md) 解释工具调用、cache、evidence ID 和 verify gates。
10. 用 [RAG、记忆与上下文技术深挖](rag-memory-context-deep-dive.md) 解释 runbook chunk、embedding 维度、memory scopes、context budget、compression 和 cache 指标边界。
11. 用 [Runbook 草稿、版本与 Amendment 生命周期技术深挖](runbook-draft-version-amendment-lifecycle-deep-dive.md) 解释 draft 来源、publish/version、chunk ingest、M9 incident diff amendment 和不自动合并/发布的边界。
12. 用 [配置、Discovery 与 EffectiveConfig 技术深挖](config-discovery-effective-config-deep-dive.md) 解释 `Settings`、M9 feature gates、discovery proposal、config publish、override TTL 和 worker 只读 published config。
13. 用 [Discovery、Capability Matrix 与服务拓扑技术深挖](discovery-capability-topology-deep-dive.md) 解释 DiscoveryRunner、backend endpoint detection、capability matrix、workload binding、service edge、rerun lock 和 pending proposal 边界。
14. 用 [Observability 与后端适配器技术深挖](observability-backend-adapters-deep-dive.md) 解释 Prometheus、Loki、Trace、Deployment、K8s、DB backend 如何接入 worker deps、cache、URL safety 和 read-only live diagnostics。
15. 用 [Deployment Change、GitHub、Argo CD 与发布变更证据技术深挖](deployment-change-github-argocd-deep-dive.md) 解释 fixture/GitHub/Argo CD 变更证据、deployment correlation 和 rollback action 边界。
16. 用 [LLM、Prompt、FakeLLM 与 Provider 边界技术深挖](llm-prompt-fakellm-provider-boundaries-deep-dive.md) 解释 provider 工厂、FakeLLM、prompt fallback、usage metadata、reasoning redaction、manual real provider eval 和 M9 draft-only 边界。
17. 用 [执行器、动作能力与验证闭环技术深挖](executor-action-verification-loop-deep-dive.md) 解释 fixture/live executor、capability metadata、snapshot、execute preflight、verify gates 和 replan。
18. 用 [报告生成、版本与事件生命周期技术深挖](report-generation-incident-lifecycle-deep-dive.md) 解释 generate_report、report regeneration、incident/run 状态同步、报告通知和前端报告页。
19. 用 [护栏与审批技术深挖](guardrail-approval-deep-dive.md) 解释批量审批、email token、stale auto-approve、snapshot 和 live executor preflight。
20. 用 [通知、邮件、评论协作与操作员交互技术深挖](notifications-collaboration-operator-interaction-deep-dive.md) 解释 email queue/log、approval email token、comments、annotations、WebSocket 和浏览器通知。
20. 用 [反馈、NFA、关联事件与持续学习技术深挖](feedback-nfa-correlation-continuous-learning-deep-dive.md) 解释 NFA、feedback、correlation、runbook amendment、memory/eval 回流和未自动接线边界。
21. 用 [前端控制台与实时更新技术深挖](frontend-realtime-console-deep-dive.md) 解释 React 控制台如何读取状态、触发审批 mutation 并用 WebSocket 事件刷新 run。
22. 展示 `configuration.md` 中的 feature gates、production defaults 和 M9 rollback。
23. 用 [测试、Eval 与工程指标技术深挖](testing-eval-engineering-metrics-deep-dive.md) 说明这些边界如何通过 pytest/Vitest/Playwright、FakeLLM smoke eval 和 `/api/evals/engineering-metrics` 固化。
24. 用 [生产发布、运维与回滚技术深挖](production-operations-rollback-deep-dive.md) 说明生产默认值、K8s overlay、健康检查、M9 rollout、live backend 和回滚验证如何落地。
