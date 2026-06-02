# Phase 6：协作与审批增强（团队化）

目标：从单人到多人。在现有审批/guardrail（见 `02-agent/guardrails-and-approval.md`）基础上支持实时协作和更灵活的审批策略。

## 6.1 实时协作

目标：多个 SRE 同时诊断同一个 incident。

| 任务 | 细节 |
| --- | --- |
| WebSocket 推送 | 诊断节点状态实时推送到前端（替代 5 秒轮询） |
| 多人标注 | 不同 SRE 可添加 comment、标注证据、@ 同事 |
| 操作审计 | 记录谁在什么时候做了什么（write-ahead log） |

## 6.2 审批流增强

目标：更灵活的审批策略。

| 任务 | 细节 |
| --- | --- |
| 审批组 | 按服务/团队分组审批权限 |
| 定时自动批准 | L2 动作 N 分钟无人响应 → 自动批准（可配置阈值） |
| 批量审批 | 同一 incident 多个 L2 动作 → 一键批量审批/拒绝 |
| 邮件审批 | 邮件中的 approve/reject 链接 → 点击直达审批页面（与 `phase-3-alerts-and-notifications.md` 协同） |

> 不变约束：无论审批策略如何放宽，L3 仍需二次确认字段（`risk_ack=true`、`confirm_action_type`、`confirm_target`），L4 仍直接拒绝。
