# 拓展计划总览

本目录把 `tzplan.md` 的拓展计划落到实现级文档，承接 `00-overview/milestones.md`（M1-M7 MVP）之后的演进路线。项目内容已按当前本地 demo 与仓库内扩展范围完成；本目录现在作为 phase-level 完成记录和后续维护背景。

> 边界提醒：完成状态不放宽强制安全边界。默认仍不操作真实生产 K8s、不做真实云资源写、L4 直接拒绝、CI/smoke 使用 FakeLLM。涉及真实生产写操作、模型微调、完整 RBAC/SSO 或其他 scope 放宽的事项，仍必须单独立项与审批。

## 当前状态

M1-M7 与 Phase 1-8 已完成当前仓库范围内的交付：

- MVP 主链路：4 类故障的告警接入、LangGraph 诊断、Mock 执行、审批、报告全流程。
- Phase 1：LLM provider factory、推理深度分层、证据交叉验证、级联故障分析。
- Phase 2：可插拔只读数据后端、K8s/DB 诊断、更多故障类型、工具缓存与审计增强。
- Phase 3：告警源适配和邮件通知闭环。
- Phase 4：Runbook 混合检索、reranker、草稿生成、版本管理与多语言配置。
- Phase 5：多级记忆、跨 incident 关联、反馈/NFA 数据沉淀。
- Phase 6：评论、证据标注、审批组、批量/邮件审批与审计日志。
- Phase 7：API key、Prometheus metrics、worker health、eval/shadow eval 和运维支撑。
- Phase 8：React 控制台增强、节点事件展示、报告/审批/评测相关页面能力。

真实 provider、Jaeger/Tempo/GitHub/Argo CD/K8s/PG 等 live 后端的启用属于环境配置和手动 demo/eval 范畴；不作为 CI 稳定门禁，也不改变默认 fixture/mock 行为。

## 阶段索引

| 阶段 | 主题 | 文档 | 状态 |
| --- | --- | --- | --- |
| 环境 | 本地开发硬件约束与 Docker 精简模式 | `local-dev-environment.md` | 完成 |
| Phase 1 | 智能诊断升级（核心能力） | `phase-1-intelligent-diagnosis.md` | 完成 |
| Phase 2 | 工具层实战化（数据打通） | `phase-2-tools-productionization.md` | 完成 |
| Phase 3 | 告警源与通知（闭环打通） | `phase-3-alerts-and-notifications.md` | 完成 |
| Phase 4 | Runbook RAG 增强（知识引擎） | `phase-4-runbook-rag.md` | 完成 |
| Phase 5 | 记忆与持续学习（长期壁垒） | `phase-5-memory-and-learning.md` | 完成 |
| Phase 6 | 协作与审批增强（团队化） | `phase-6-collaboration-and-approval.md` | 完成 |
| Phase 7 | 运维与工程化（生产化） | `phase-7-ops-and-engineering.md` | 完成 |
| Phase 8 | 前端增强（体验优化） | `phase-8-frontend.md` | 完成 |

## 完成口径

| 里程碑 | 交付内容 | 完成口径 | 状态 |
| --- | --- | --- | --- |
| M1-M7 | MVP 端到端链路 | FakeLLM、fixture/mock 数据、审批阻断、报告和测试门禁可复现 | 完成 |
| P1 | LLM Provider factory + 推理分层 + 证据校验 | adapter 与配置切换完成；FakeLLM 保持确定性；真实 provider 仅手动启用 | 完成 |
| P2 | 工具层后端化 + 只读 K8s/DB + 扩展故障类型 | 默认 fixture/read-only；无真实生产写操作；工具审计和缓存保留 | 完成 |
| P3 | 告警和邮件通知闭环 | 多来源告警适配、异步邮件、审批/报告链接和失败日志 | 完成 |
| P4 | Runbook RAG 增强 | 混合检索、rerank、草稿、版本、多语言 embedding 配置 | 完成 |
| P5 | 记忆与学习 | 反馈、NFA、跨事故关联、记忆事件和 cache/compression 指标 | 完成 |
| P6 | 协作和审批增强 | 评论、证据标注、审批组、批量/邮件审批、审计日志 | 完成 |
| P7 | 运维与工程化 | API key、Prometheus metrics、worker health、eval/shadow eval | 完成 |
| P8 | 前端增强 | 事故、运行、审批、报告、Runbook、eval 等控制台流程 | 完成 |

## 保留边界与运行风险

| 边界 / 风险 | 当前处理 |
| --- | --- |
| FakeLLM 与真实 provider | CI/smoke 固定 FakeLLM；真实 provider 通过配置手动 demo/eval，不作为稳定门禁。 |
| 真实数据源 | Trace/Git/K8s/DB 提供可配置只读后端；默认 fixture，live smoke 按环境单独执行。 |
| 生产写操作 | 不实现真实生产 K8s/cloud/DB/cache 写操作；L4 直接拒绝。 |
| 认证授权 | 当前以 API key 作为本地 demo 与过渡鉴权；完整企业 RBAC/SSO 属于单独生产化项目。 |
| 审批策略 | L2/L3 仍需审批；L3 保留二次确认字段；L4 不进入审批。 |
| 外部模型变化 | provider/model/价格/thinking 参数保持配置化，手动 demo 前需复核供应商文档。 |
| 前端实时性 | WebSocket 节点事件已纳入，前端可保留轮询作为降级路径。 |
