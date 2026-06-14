# 文档更新批次计划

**最后更新：** 2026-06-14

目标：让开发者只通过文档就能系统了解项目的目标、架构、数据模型、运行路径、安全边界、开发方式、测试方式、部署方式和运维方式。

本文是文档工作的批次化路线。它不替代各专题文档；每一批的输出都应落到对应 `docs/` 文件中。

## 原则

- 小批次更新，每批有清晰主题、验收标准和受影响文档。
- 先补导航和阅读路径，再补深水区细节。
- 文档描述当前代码行为；历史 `plans/` 只作背景。
- 不通过文档放松安全边界，不把 roadmap 能力写成默认可用能力。
- 每次行为变更都同步更新模块文档、配置参考和测试说明。
- 优先补开发者会实际查找的路径：从告警到报告、从 API 到 DB、从节点到工具、从配置到运行时效果。

## 批次总览

| 批次 | 主题 | 主要文档 | 验收标准 | 状态 |
|------|------|----------|----------|------|
| B0 | 文档入口与开发者导航 | `docs/README.md`、`developer-guide.md`、本文 | 新开发者能找到阅读顺序、源文档优先级、改动入口 | 已完成 |
| B1 | 总览、架构和边界校准 | `project-overview.md`、`architecture.md`、`scope-and-boundaries.md`、`repository-map.md` | 能画出主链路、层级架构、配置优先级和安全边界 | 已完成 |
| B2 | 后端、API、数据模型 | `backend-architecture.md`、`api-reference.md`、`data-model.md`、`errors-and-request-ids.md`、`auth-and-api-keys.md`、`celery-and-jobs.md` | 能按文档实现或调试一个 API 端到端改动 | 已完成 |
| B3 | Agent、工具、RAG、记忆 | `workflow.md`、`guardrails-and-approval.md`、`llm-and-prompts.md`、`tool-layer.md`、`runbook-rag.md`、`memory-cache-compression.md` | 能按文档安全新增一个节点、工具或检索能力 | 已完成 |
| B4 | 前端、演示和本地体验 | `react-console.md`、`quick-start.md`、`local-demo.md`、`demo-playbook.md` | 能跑通本地 demo、定位页面数据来源、验证审批流程 | 已完成 |
| B5 | 测试、评估、运维、生产 | `testing-strategy.md`、`evaluation.md`、`development-workflow.md`、`runbook.md`、`production-checklist.md`、`final-pre-execution-checklist.md` | 能判断一个变更需要哪些测试、如何发布和回滚 | 已完成 |
| B6 | M9 与参考资料深化 | `m9-rollout.md`、`m9-data-flow.md`、`m9-threat-model.md`、`configuration.md`、`status-and-ids.md`、`glossary.md` | 能理解 M9 默认关闭、独立回滚、外部调用安全和配置含义 | 已完成 |

## 每批工作流

1. 读取对应专题文档、相关代码目录、测试文件和 `AGENTS.md` 边界。
2. 对照当前代码修正文档中过期的数量、路径、配置、状态、风险等级和默认值。
3. 补齐开发者最需要的内容：入口、数据流、状态机、错误路径、测试入口、常见误区。
4. 保持模块边界清晰，不把实现细节复制到多个文档造成二义性。
5. 更新 `docs/README.md` 或根 `README.md` 的索引，只在新增文档或入口发生变化时更新。
6. 运行适合文档变更的验证：链接人工检查、命令/API 对照代码、必要时运行相关测试。

## B0 输出

B0 只解决“怎么读”和“从哪里改”的问题：

- 新增 [开发者全景指南](developer-guide.md)。
- 新增本文档，作为后续文档批次的执行清单。
- 在文档中心和根 README 中加入入口。
- 明确 `docs/` 优先于历史 `plans/`，避免旧规划覆盖当前实现。

## B1 计划与输出

目标：让读者能在 30 分钟内理解系统主链路和安全边界。

待检查内容：

- `docs/00-overview/project-overview.md` 是否准确描述当前完成状态和测试规模。
- `docs/00-overview/architecture.md` 是否覆盖 API、worker、LangGraph、工具层、DB、前端和 M9 数据流。
- `docs/00-overview/scope-and-boundaries.md` 是否明确 fixture 默认、live executor opt-in、只读诊断、L2/L3/L4 行为。
- `docs/00-overview/repository-map.md` 是否与当前目录结构、模块数量和测试布局一致。

验收：

- 文档能回答“告警如何变成报告”“哪些路径可能真实写入外部系统”“生产环境默认关闭什么”。
- 不出现旧 MVP 限制覆盖当前实现的表述。
- 不出现放宽 M9 或 live executor 安全边界的描述。

B1 已完成内容：

- `project-overview.md` 补齐当前仓库快照、主链路、核心能力和读者必须先记住的边界。
- `architecture.md` 补齐端到端架构图、运行时数据流、存储/checkpoint、默认 compose 服务和 M9 增强位置。
- `scope-and-boundaries.md` 补齐默认安全姿态、读写边界、允许的 live K8s 写路径、禁止项、风险等级和 M9 安全不变量。
- `repository-map.md` 校准当前目录规模、应用层/共享库/测试/deploy/demo/docs/plans 职责和常见定位路径。

## B2 计划与输出

目标：让读者能从 HTTP 请求追踪到数据库写入和 Celery 任务。

待检查内容：

- API 端点分组、请求/响应示例、错误结构、`X-Request-Id` 行为。
- alert ingestion、fingerprint 去重、diagnose 入队、approval resume、report regeneration。
- router/service/repository 边界和事务归属。
- SQLAlchemy model、迁移、ID 前缀、状态机、JSONB/vector 字段。
- API key、scope、rate limit、WebSocket 事件。

验收：

- 开发者能按文档新增一个端点并知道要改 schema、service、repository、测试和 API 参考。
- 数据模型文档不遗漏新增模型、关键约束和迁移语义。

B2 已完成内容：

- `backend-architecture.md` 补齐 router/service/repository/schema/依赖注入、核心请求流程、事务和新增后端能力落点。
- `api-reference.md` 校准 76 条业务 HTTP route + 1 条 WebSocket，并按分组列出 endpoint、认证、scope、关键请求/响应和实现差异。
- `data-model.md` 按当前 32 个 ORM 模型重分组，补齐 `EmailLog`、embedding side table、poll cursor、约束和迁移清单。
- `errors-and-request-ids.md` 补齐 request ID middleware、标准错误信封、HTTPException 例外、rate limit 和审计日志说明。
- `auth-and-api-keys.md` 补齐 middleware 流程、开放路径、WebSocket token、scope enforcement 现状和 bootstrap key 注意事项。
- `celery-and-jobs.md` 补齐 Celery 配置、task 清单、Beat 调度、诊断/恢复幂等、checkpoint、discovery/poll/eval 任务。

## B3 计划与输出

目标：让读者能安全理解和扩展诊断能力。

待检查内容：

- LangGraph 节点顺序、条件路由、checkpoint/resume、GraphInterrupt。
- `IncidentState` 字段、内部字段剥离、node trace 和 tool call 记录。
- Guardrail 分类、审批二次确认、执行前快照、验证/重新规划循环。
- 工具 query/result schema、缓存、超时、降级、审计摘要。
- RAG ingest/split/embed/retrieve/rerank、语义搜索 feature gate、LLM 草稿限制。
- Memory L0-L3、token budget、compression trigger、provider/app cache 指标拆分。

验收：

- 开发者能按文档新增一个工具或 Agent 节点，并知道如何避免真实外部写入和大日志入 prompt。

B3 已完成内容：

- `workflow.md` 按当前 18 节点 LangGraph 图重写，补齐并行证据采集、条件路由、checkpoint/resume、state 字段、node trace/tool call 记录和新增节点 checklist。
- `guardrails-and-approval.md` 校准 L0-L4 当前动作表、未知动作/L4 禁用词、L3 二次确认、无 checkpointer dev/test 行为、stale auto-approve 默认关闭和 live executor 限制。
- `llm-and-prompts.md` 校准 provider 工厂、FakeLLM 15 类告警覆盖、当前 LLM 默认值、multi-perspective、reasoning 元数据剥离和 M9 LLM 草稿边界。
- `tool-layer.md` 校准 15 个工具模块、同步 `run(query)` 协议、`ToolResult`、缓存桶、各 backend 降级行为和新增工具 checklist。
- `runbook-rag.md` 校准 20 个 RAG 模块、splitter 默认 450/900/80、embedding/reranker provider、hybrid 检索、draft/version/amendment 和 M9 Web/LLM/external embedding 边界。
- `memory-cache-compression.md` 校准 7 个 memory 模块、L0-L3 读取/写入、默认 token budget、当前 compression event 触发条件、压缩策略和 provider/app cache 指标拆分。

## B4 计划与输出

目标：让读者能跑通本地体验并定位前端页面数据来源。

待检查内容：

- React 页面路由、TanStack Query key、轮询和 WebSocket 使用方式。
- loading/empty/error/conflict 状态。
- L3 二次确认 UI、approval list、agent run 节点轨迹、cache/token/compression 展示。
- Docker Compose 服务、端口、fixture 数据、demo fault 注入路径。

验收：

- 开发者能用文档从本地启动到触发告警、查看诊断、审批动作、阅读报告。

B4 已完成内容：

- `react-console.md` 校准 React 19/Router 7/TanStack Query 5 当前栈，补齐路由、API client、query key、轮询、WebSocket、审批弹窗、通知、报告页、测试入口和新增页面 checklist。
- `quick-start.md` 重写本地启动路径，区分完整 Compose 与手动开发模式，补齐 13 个默认服务、mailpit dev profile、宿主机端口映射、runbook ingest、4 个 demo 告警 fixture 和认证提示。
- `local-demo.md` 校准 `docker-compose.yml` 当前服务、端口、profile、环境变量、observability provisioning、demo-service fault endpoint、扩缩容和安全边界。
- `demo-playbook.md` 按准备、触发、观察、审批、报告、清理顺序重写，覆盖 4 个现成场景、L2/L3 审批演示、扩展 FakeLLM alert_name 和 M9 手动演示边界。
- 根 `README.md` 校准快速开始和前端技术栈口径，避免把一键 Compose 与手动 API 启动混用。`docs/operator-runbook.md` 小范围修正 Compose 默认服务数量注释。

## B5 计划与输出

目标：让读者能判断变更风险、选择测试层级、执行发布前检查。

待检查内容：

- 后端、前端、E2E、contract、manual eval 的职责划分。
- FakeLLM、fixture executor、live backend opt-in 的测试约束。
- smoke eval 与 manual full eval 的边界。
- 开发工作流、CI 步骤、生产 checklist、运维 runbook、回滚开关。

验收：

- 文档能回答“这个变更要跑哪些测试”“失败时如何降级/回滚”“哪些能力不能进 CI 稳定门禁”。

B5 已完成内容：

- `testing-strategy.md` 校准 100 个 Python test 文件、前端 31 个 unit/API tests + 1 个 Playwright smoke、CI job、coverage 硬门禁、测试 fixture 默认值、测试层级选择和关键行为测试映射。
- `evaluation.md` 校准 eval 模块、smoke 4 cases、full 20 cases、`run_suite` 输出、CI smoke 指标、API/Celery eval path、replay/shadow 非门禁边界和真实 provider 手动使用规则。
- `development-workflow.md` 重写读文档入口、本地环境、CI 等价命令、测试选择、迁移、依赖、前端开发和 PR checklist。
- `runbook.md` 重写本地/预生产运维流程，补齐 `/readyz` 实际响应、Compose 13 默认服务、worker/approval/runbook 排障、`agentp_*` 指标和回滚命令。
- `production-checklist.md` 重写生产 P0/P1/P2，区分代码自动生产默认值和上线必须显式确认的安全配置，补齐 live backend、M9 和发布记录要求。
- `final-pre-execution-checklist.md` 重写最终门禁顺序，覆盖 baseline、CI/eval、安全 P0、生产功能、M9、回滚和 24 小时观察。
- `operator-runbook.md` 作为 Day-2 运维入口同步校准服务拓扑、健康检查、指标、M9 操作和禁止项；根 `README.md` 小范围校准测试数量与门禁口径。

## B6 计划与输出

目标：让读者能查到所有稳定术语、状态、配置和 M9 控制面。

待检查内容：

- `configuration.md` 中的默认值、feature flag、生产安全默认值、回滚变量。
- `status-and-ids.md` 中的 ID 前缀、状态枚举、生命周期。
- `glossary.md` 中的核心术语、M9 术语和安全术语。
- M9 数据流、威胁模型、rollout、rollback 是否相互一致。

验收：

- 开发者能凭参考文档判断某个配置是否生产默认关闭、是否允许外部调用、失败时如何降级。

B6 已完成内容：

- `m9-rollout.md` 按当前 feature flag resolver、LLM/Web/Tempo/Grafana/semantic/external embedding 行为重写，补齐每个子能力的启用顺序、scope、降级、指标和回滚。
- `m9-data-flow.md` 按 8 条 M9 数据流重写，区分通用 `/api/alerts` 的 Grafana 规范化与 M9 Grafana webhook helper，补齐 redaction、持久化和失败路径。
- `m9-threat-model.md` 重写资产、信任边界、secret leakage、prompt injection、SSRF、外部调用、runbook 发布、Tempo/Grafana/external embedding 风险与生产安全 checklist。
- `configuration.md` 按当前 `Settings` 默认值重写，覆盖 Compose 差异、配置优先级、工具后端、executor、RAG/LLM/M9、认证、告警轮询、通知、HA 和生产 minimums。
- `status-and-ids.md` 校准当前 public ID 前缀、公共状态 enum、runbook/discovery/config/email/eval 状态和 legacy/test-only 前缀说明。
- `glossary.md` 重写核心架构、Agent、工具、审批、RAG、记忆、M9、安全、测试和运维术语。

## 完成定义

每个批次完成时应满足：

- 对应文档已更新到当前代码行为。
- 新增或变更的文档入口已接入 `docs/README.md`。
- 安全边界与 `AGENTS.md` 一致。
- 没有把旧 `plans/` 中的过期口径写回当前文档。
- 行为、配置或 API 改动已有对应测试说明。
- 最终说明中列出已改文档、未验证项和下一批建议。
