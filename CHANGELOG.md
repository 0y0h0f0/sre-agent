# 变更日志

SRE 事件响应 Agent 的所有重要变更。

## [M9] — 2026-06-12（进行中）

受控增强阶段。所有功能均受 `M9_EXTENSIONS_ENABLED=false` 控制（生产环境默认关闭）。

### PR 9.9 — 外部 Embedding 提供商 (`ca9c90e`)
- 外部 embedding 提供商支持，由 `EXTERNAL_EMBEDDING_PROVIDER_ENABLED` 控制
- 需要 `config:write` + `embedding:external` 权限范围
- 提供商不可用时进行降级回退

### PR 9.8 — 语义 Runbook 搜索 (`9dbd4fa`)
- 关键词、语义及混合 runbook 搜索，由 `SEMANTIC_RUNBOOK_SEARCH_ENABLED` 控制
- 基于 embedding 的 pgvector 搜索
- 渐进式回退：语义搜索 -> 关键词搜索 -> 子串搜索

### PR 9.7 — Grafana Webhook 解析器增强 (`ce0c151`)
- Grafana 统一告警 webhook 接入，由 `GRAFANA_ALERT_INGEST_ENABLED` 控制
- 需要 HMAC 签名验证
- 生成稳定的指纹以支持去重

### PR 9.6 — Tempo 自动发现启用 (`fbb04f7`)
- Tempo 端点自动发现，由 `TEMPO_DISCOVERY_ENABLED` 控制
- 生产环境绝不自动发布 Tempo 端点
- 发现的端点需要显式审核

### PR 9.4 + 9.5 — Web 搜索安全 + TempoTraceBackend (`5b46249`)
- Runbook web 搜索安全：超时、最大结果数、HTTPS 要求、域名允许/阻止列表、缓存 TTL
- Tempo trace 后端适配器（`TRACE_BACKEND=tempo`）
- 所有外部调用均有超时、脱敏、审计和降级回退

### PR 9.3 — LLM 事件差异分析
- LLM 驱动的事件与 runbook 差异分析，由 `LLM_INCIDENT_DIFF_ENABLED` 控制
- 仅创建 `AmendmentDraft(status=pending_review)`
- 绝不自动批准、自动发布或自动应用

### PR 9.2 — LLM Runbook 草稿生成
- LLM 驱动的 runbook 草稿生成，由 `RUNBOOK_LLM_GENERATION_ENABLED` 控制
- 仅创建 `RunbookDraft(status=pending_review)`
- LLM 不可用时使用确定性回退

### PR 9.1 — M9 功能开关
- 全局 `M9_EXTENSIONS_ENABLED` 功能开关（生产环境默认 `false`）
- `PRE_M9_TRACE_BACKEND` / `PRE_M9_TRACE_ENABLED` 回滚状态
- 所有 M9 功能均有独立的子功能开关
- 仅增强：绝不替代 M0–M8 的确定性路径

---

## [M0–M8] — 2026-06-07 至 2026-06-12

真实后端集成。跨 8 个里程碑的 41 个 PR。所有诊断和 runbook 功能均为确定性的。

### M8 — 生产安全与发布关卡
- 生产安全测试和 E2E 测试
- 发布关卡文档
- M9 之前最终加固

### M7 — Runbook 反馈与确定性分析
- 确定性 runbook 反馈分析
- Runbook 反馈模型和修订设置
- 反馈循环无真实 LLM 依赖

### M6 — Runbook 模板引擎
- Runbook 模板引擎，含草稿类型/来源追踪
- Runbook 审核 API
- 使用确定性方法生成模板

### M5 — 发现配置 API 与 Worker 集成
- 发现配置 API，含带权限范围的运维密钥
- Worker 与 EffectiveConfig 集成
- Alertmanager 轮询任务，含 Redis 锁和已解决状态推断
- `config:write` 和 `discovery:write` 权限范围

### M4 — 发现执行器与生效配置
- 后端自动检测的发现执行器
- EffectiveConfigVersion 发布流程
- 配置合并：`env > active override > profile > published > safe default`
- Alertmanager 轮询，含保守的已解决状态推断

### M3 — 后端发现
- Prometheus、K8s、Loki、Jaeger 和拓扑发现
- DiscoveryRun -> DiscoveryProposal 链路
- 仅检测配置与已发布配置的分离

### M2 — 真实后端适配器
- 真实 Prometheus、Loki、Jaeger 适配器
- 后端 URL 安全验证
- 各后端认证（bearer token、basic、mTLS）

### M1 — 后端认证与安全
- BackendAuthConfig，含密钥引用（`env:VAR_NAME`）
- 后端 URL 允许列表验证
- 原始密钥绝不存储在数据库、审计日志或 LLM 上下文中

### M0 — 生产安全基础
- `APP_ENV` 安全默认值（`local` 与 `production`）
- 生产环境中 `LLM_PROVIDER=disabled`
- `EXECUTOR_BACKEND=fixture` 默认值
- 生产环境无 localhost 回退

### 附加功能（M0–M8 期间）

- **ReAct 验证循环** — 快照/回滚、验证/重新规划周期
- **证据交叉验证** — metrics/logs/traces/deployment 信号融合
- **级联故障分析** — 服务依赖图谱及传播分析
- **K8s executor** — 重启/扩缩容/回滚变更（仅按需启用真实 executor）
- **LangGraph checkpointing** — 使用 PostgresSaver 的 PostgreSQL 持久化
- **上下文压缩** — 当日志超过 20 条或超过 3000 token 时触发
- **审计日志** — 不可变，禁止更新/删除，数据库触发器强制执行
- **API 密钥** — 角色（operator、admin）和权限范围（config:write、discovery:write、api_key:admin）
- **错误信封** — 一致的 `{"error": {"code", "message", "request_id", "details"}}` 格式
- **WebSocket 节点事件** — agent 运行的实时进度
- **邮件通知** — 使用 Jinja2 模板的 SMTP
- **Runbook 版本** — 重新生成创建新版本，绝不覆盖
- **指纹去重** — 针对未关闭事件的数据库级约束
- **工具缓存** — UTC 时间桶（metrics/logs：1 分钟，traces：5 分钟，git：10 分钟）
- **记忆系统** — L0 运行本地、L1 事件、L2 服务、L3 流程
- **Celery Beat** — 定时任务（告警轮询、清理）

---

## [初始版本] — 2026-06-07 及更早

核心 SRE 事件响应 Agent，具备本地演示能力。

### 阶段 3 — 全面集成
- React + TypeScript + Vite 控制台
- TanStack Query API 状态管理
- 审批 UI，含 L3 二次确认
- 使用 Playwright 的 E2E 测试

### 阶段 2 — Agent 核心
- LangGraph 工作流（从 parse_alert 到 persist_memory）
- FakeLLM 适配器
- 护栏和审批系统
- 模拟 executor 和 fixture 后端

### 阶段 1 — 基础
- FastAPI 应用，含路由和服务
- SQLAlchemy 模型和 Alembic 迁移
- Pydantic schema 和验证
- Celery 异步任务处理
- 使用 pgvector embedding 的 Runbook RAG
- 智能诊断升级和护栏加固
- 审批护栏绕过修复
- 事件生命周期边界情况修复

### 初始搭建
- 项目结构：apps/、packages/、tests/、demo/、deploy/
- 固定技术栈：FastAPI、LangGraph、Celery、PostgreSQL、pgvector、Redis
- 文档框架
- CI 流水线（ruff、mypy、pytest、vitest、Playwright）
