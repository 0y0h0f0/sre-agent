# Phase 5：记忆与持续学习（长期壁垒）

目标：从固定到自进化。在现有多级记忆（见 `05-memory/memory-implementation.md`）基础上做跨 incident 关联、误报学习与用户反馈回路。

> 边界：MVP scope 明确不做模型微调（见 `00-overview/scope.md`）。本阶段只沉淀可审计反馈数据，模型微调需单独立项、数据治理和人工审批，不自动触发。

## 5.1 跨 Incident 关联

目标：相似故障自动关联，复用诊断结论。

| 任务 | 细节 |
| --- | --- |
| Incident 聚类 | 相同 fingerprint → 关联；相似 embedding → 推荐关联 |
| 诊断复用 | 新 incident 与历史相似 → 直接推荐历史根因 + 处置动作 |
| 关联图 UI | 前端展示时间线上的关联 incident 网络 |

## 5.2 误报学习

目标：标记 Noise / False Positive，降低同类告警干扰。

| 任务 | 细节 |
| --- | --- |
| 标注机制 | 前端 NFA（Not an Actionable Alert）按钮 → 记录 fingerprint |
| 自动降级 | 同一 fingerprint 3 次标记 NFA → 自动降为 P4 / suppressed |
| 恢复策略 | 30 天未出现 → 重置计数；或人工恢复到正常级别 |

## 5.3 用户反馈回路

目标：SRE 修正诊断结果后，系统把反馈转化为评测样本、runbook 更新和策略改进。

| 任务 | 细节 |
| --- | --- |
| 根因修正 | SRE 可改写根因 → 记录原始 vs 修正 delta → 进入 eval dataset 和 runbook 草稿候选 |
| 动作修正 | SRE 添加/删除建议动作 → 更新 deterministic guardrail / plan_actions prompt / action 模板 |
| 学习边界 | 只沉淀可审计反馈数据；模型微调需单独立项、数据治理和人工审批，不自动触发 |
