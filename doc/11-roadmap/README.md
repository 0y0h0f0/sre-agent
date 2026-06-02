# 拓展计划总览

本目录把 `tzplan.md` 的拓展计划落到实现级文档，承接 `00-overview/milestones.md`（M1-M7 MVP）之后的演进路线。MVP 已完成的范围见 `00-overview/scope.md`；本目录描述的是从 Demo 到生产级 SRE Agent 的后续阶段。

> 边界提醒：本目录是**计划**，不是已交付功能。每个阶段落地前需复核 `00-overview/scope.md` 的强制边界（不操作真实生产 K8s、不做真实云资源写、L4 直接拒绝、覆盖率 > 80%）。涉及放宽 scope 的项（K8s 写、RBAC、模型微调）必须单独立项与审批。

## 当前状态

M1-M7 全部完成，MVP 已具备：

- 4 类故障的告警接入、LangGraph 诊断、Mock 执行、审批、报告全流程。
- PostgreSQL + pgvector、Redis、Celery、Prometheus、Loki、OTel。
- FastAPI 后端 + React 前端控制台。
- FakeLLM 确定性诊断 + Mock Executor。

MVP 明确限制（`00-overview/scope.md`）：单租户、1 个 demo-service、4 类固定故障；不做 RBAC/SSO、不操作真实云资源、不做模型微调、不删数据。

**进行中**：
- Phase 1.1（LLM Provider factory）已落地——`packages/agent/llm/` 提供 fake / vllm / openai / deepseek / anthropic adapter，`_build_deps()` 经 `build_llm(settings)` 构造，`llm_provider=fake` 保持全部测试确定性。
- Phase 1.2（推理深度分层）已落地——`packages/agent/llm/reasoning.py` 按 `llm_reasoning_nodes` 配置逐节点开启深度推理，`diagnose` 产出可审计 rationale 并记录 LLM 调用元数据，不持久化原始 CoT。
- Phase 1.3（证据交叉验证）已落地——`packages/agent/evidence_validation.py` 融合 metrics/logs/traces/deployment 信号，多源印证提升置信度、信号矛盾置 `needs_human_review`、缺失源降级不中断。
- Phase 1.4（级联故障分析）已落地——`packages/agent/topology.py` 提供服务依赖图、故障传播建模（根服务定位）与批量 incident 关联提级；`diagnose` 接入 `cascade_analysis`（informational）。

**Phase 1 全部子项（1.1-1.4）已落地**。详见 `phase-1-intelligent-diagnosis.md` 的实现状态。待真实环境验证项：vLLM/云端 smoke 与深度推理延迟实测、多服务真实拓扑下的级联与批量关联（依赖 Phase 2 真实数据源接入）。

## 阶段索引

| 阶段 | 主题 | 文档 |
| --- | --- | --- |
| 环境 | 本地开发硬件约束与 Docker 精简模式 | `local-dev-environment.md` |
| Phase 1 | 智能诊断升级（核心能力） | `phase-1-intelligent-diagnosis.md` |
| Phase 2 | 工具层实战化（数据打通） | `phase-2-tools-productionization.md` |
| Phase 3 | 告警源与通知（闭环打通） | `phase-3-alerts-and-notifications.md` |
| Phase 4 | Runbook RAG 增强（知识引擎） | `phase-4-runbook-rag.md` |
| Phase 5 | 记忆与持续学习（长期壁垒） | `phase-5-memory-and-learning.md` |
| Phase 6 | 协作与审批增强（团队化） | `phase-6-collaboration-and-approval.md` |
| Phase 7 | 运维与工程化（生产化） | `phase-7-ops-and-engineering.md` |
| Phase 8 | 前端增强（体验优化） | `phase-8-frontend.md` |

## 优先级排序

```text
Phase 1: 智能诊断升级     ← 核心能力，从 Demo 到有用
Phase 2: 工具层实战化     ← 数据打通，从 Mock 到真实
Phase 3: 告警源与通知     ← 闭环打通，从被动查到主动推
Phase 4: Runbook RAG 增强  ← 知识引擎，从检索到理解
Phase 5: 记忆与持续学习    ← 长期壁垒，从固定到自进化
Phase 6: 协作与审批增强    ← 团队化，从单人到多人
Phase 7: 运维与工程化      ← 生产化，从 Demo 到生产级
Phase 8: 前端增强          ← 体验优化，从能用到好用
```

## 关键里程碑

| 里程碑 | 交付内容 | 验收标准 | 预计工作量 |
| --- | --- | --- | --- |
| P1 完成 | LLM Provider factory + Qwen2.5-7B 本地 smoke + 云端 API 适配 + 推理分层 | `fake` 测试保持确定性通过；`vllm` 端到端 smoke 通过；diagnose 输出结构化 rationale | 3-4 周 |
| P2 完成 | Prometheus/Loki 查询增强 + Trace/Git 真实适配器 + K8s 只读 + 5 种新故障 | 至少 1 个非 demo 可观测后端接入；生产 K8s 写操作默认不可执行 | 3-4 周 |
| P3 完成 | Alertmanager 对接 + 邮件通知完整闭环 | 一条真实告警 → 自动诊断 → 邮件推送；审批邮件能打开指定 approval | 1-2 周 |
| P4 完成 | 混合检索 + Reranker | 4 类 MVP 故障 Runbook Top-3 命中率 > 80% | 1-2 周 |
| P1-P4 累计 | 完整智能诊断 + 知识引擎 | 端到端智能化水平达到可试用状态 | 8-12 周 |
| P1-P8 累计 | 生产级 SRE Agent 平台 | 可部署到团队真实环境日常使用 | 12-20 周 |

## 技术债务与风险

| 债务 / 风险 | 说明 | 缓解措施 |
| --- | --- | --- |
| FakeLLM 是最大瓶颈 | 当前所有"诊断"都是固定规则，无泛化能力 | P1 优先解决 |
| 真实数据源仍不完整 | Prometheus/Loki 已是 HTTP 查询，但 Trace 和 Git/deployment 仍依赖 fixture，生产 label/tenant 映射也未完善 | P2 优先替换 Trace/Git fixture，并接入至少 1 个非 demo 可观测后端 |
| 单点故障 | API/Worker/DB 均单实例 | P7 做高可用，期间至少加 healthcheck + restart |
| 无认证 | 任何人可访问 API 和前端 | P7 加 RBAC，期间至少加 API Key |
| 审批逻辑简单 | L2/L3 规则硬编码，无法按服务定制 | P6 做审批组 + 可配置策略 |
| K8s 写操作边界 | scope 禁止真实生产 K8s 写操作，但拓展计划包含 rollout/scale/undo | P2 只默认启用只读；写操作限定 staging/dry-run + 审批 + 审计 |
| 外部模型变化 | 云端模型名、thinking 参数、价格和缓存策略会随时间变化 | P1 实施前复核官方模型清单和价格，adapter 保留配置化映射 |
| 前端轮询 | 5 秒轮询浪费资源且延迟高 | P8 换 WebSocket 实时推送 |
