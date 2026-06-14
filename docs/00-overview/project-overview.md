# 项目概览

**最后更新：** 2026-06-13

## 目标

SRE Incident Response Agent 是一个面向本地演示和受控生产化切片的 incident 诊断与响应系统。它接收告警，创建 incident 和 agent run，通过 Celery 异步运行 LangGraph 诊断流程，收集可追溯证据，输出根因、建议动作、审批请求、执行结果和事故报告。

系统的默认路径是安全的：本地和 CI 使用 FakeLLM、fixture 工具数据和 fixture executor。真实外部读后端和 live Kubernetes executor 只在显式配置后启用，并受确定性 guardrail 与人工审批约束。

## 当前状态

- **M0-M8 已完成**：真实后端集成、确定性诊断、安全发布、配置合并、审计、回滚、runbook 审查、Alertmanager 轮询、服务发现、证据验证、级联故障分析、K8s executor、评估、Agent 编排和 ReAct 验证循环。
- **M9 受控增强默认关闭**：LLM runbook 草稿、incident diff、web 搜索安全、Tempo trace 后端、Grafana webhook 摄取、语义 runbook 搜索和外部 embedding provider 均在显式 feature gate 后使用。生产环境默认关闭。
- **文档源关系**：`docs/` 描述当前行为；`plans/` 和 `plans/11-roadmap/` 是历史背景和 roadmap，不覆盖当前代码和安全边界。

## 当前仓库快照

以下统计来自 2026-06-13 的当前仓库结构、路由装饰器、SQLAlchemy 模型和 Docker Compose 配置。

| 维度 | 当前值 | 说明 |
|------|--------|------|
| API | 14 个 router，76 个 HTTP route，1 个 WebSocket | 详细契约见 [API 参考](../01-backend/api-reference.md) |
| 数据模型 | 32 个 SQLAlchemy 模型，15 个 Alembic 迁移 | 当前 embedding 字段为 512 维 |
| Agent 图 | 18 个 LangGraph 节点 | 含 collect-gap 与 verify/replan 两个有界循环 |
| 代码布局 | `apps/` 约 60 个 Python 文件，`packages/` 约 161 个 Python 文件 | 不计 `__pycache__`、构建产物和依赖目录 |
| 测试布局 | `tests/` 约 102 个 Python 测试文件 | 另有前端 Vitest 和 Playwright 配置 |
| 本地服务 | 默认 Compose 13 个服务；`mailpit` 为 `dev` profile 可选服务 | `docker compose --profile dev up mailpit` 可启用本地邮件 UI |
| Demo 数据 | 4 个告警 fixture、4 个故障 fixture、12 个 runbook | 位于 `demo/` |

## 核心能力

| 能力 | 当前行为 |
|------|----------|
| 告警摄取 | `POST /api/alerts` webhook 与 Alertmanager 轮询；按 fingerprint 去重开放 incident |
| 异步诊断 | API 创建 `Incident` 和 `AgentRun` 后只入队 Celery；LangGraph 不在请求线程中运行 |
| 证据收集 | Metrics、logs、traces、deployment、K8s、DB、runbook、memory、cross-incident context |
| 根因分析 | FakeLLM/真实 provider 适配器 + 确定性回退；诊断结果保留 evidence/runbook chunk 引用 |
| 动作治理 | Deterministic guardrail 将动作分类为 L0-L4；模型输出不能绕过最终权限判断 |
| 审批 | L2/L3 需要人工审批；L3 需要 `risk_ack`、`confirm_action_type`、`confirm_target` |
| 执行 | 默认 fixture executor；`EXECUTOR_BACKEND=live` 仅允许受限 Kubernetes restart/scale/rollback |
| 验证与报告 | 执行前快照、执行后 verify/replan、版本化 incident report、记忆持久化 |
| 前端 | React 控制台展示 incidents、runs、node traces、approvals、reports 和配置/发现相关信息 |
| M9 | AI/Web/Tempo/Grafana/semantic search 能力均在 `M9_EXTENSIONS_ENABLED` 和子开关后面 |

## 主链路

```text
Alert Webhook / Alertmanager Poll
  -> FastAPI: 标准化告警、fingerprint 去重、创建 Incident + AgentRun
  -> Redis/Celery: 异步调度诊断任务
  -> Worker: 读取已发布配置、构建 AgentDeps、初始化 LangGraph checkpoint
  -> LangGraph: 收集证据、检索 runbook/memory、诊断、压缩、排序、规划动作
  -> Guardrail: L0/L1 自动，L2/L3 审批，L4 拒绝
  -> Executor: 默认 fixture；显式 live K8s 仅执行允许的 Kubernetes mutation
  -> Verify/Report: 验证效果、必要时重新规划、生成报告、持久化记忆
  -> React Console: 展示状态、审批、节点轨迹、报告和实时事件
```

## 读者应先记住的边界

- 默认 executor 是 `fixture`；真实 Kubernetes 写操作必须显式选择 `EXECUTOR_BACKEND=live`。
- Live executor 只允许 Deployment rolling restart、Deployment scale/scale back、Deployment rollback subresource。
- Live K8s diagnostics 与 live DB diagnostics 是只读诊断能力，不等于写入权限。
- L4 destructive action 直接拒绝，不进入审批。
- CI、unit tests 和 smoke eval 使用 FakeLLM，不依赖真实 LLM provider。
- M9 外部调用必须有 feature flag、超时、脱敏、审计/指标、错误降级和回滚开关。

## 相关入口

- [开发者全景指南](developer-guide.md) — 阅读顺序和改动入口。
- [系统架构](architecture.md) — 分层、数据流和运行时依赖。
- [范围与安全边界](scope-and-boundaries.md) — 哪些能力可用、哪些动作禁止。
- [仓库地图](repository-map.md) — 代码和文档目录职责。
- [M9 上线计划](../m9-rollout.md) — M9 feature gate、验证和回滚。
