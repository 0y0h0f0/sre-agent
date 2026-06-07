# 范围与安全边界

## MVP 范围

MVP 是单租户、本地 demo 系统。核心目标是展示事故诊断、证据聚合、审批阻断、mock 执行和报告生成的完整链路。

MVP 支持 4 类初始事故：

- database connection exhaustion
- high 5xx after deploy
- Redis cache avalanche
- Pod restart loop with mock Kubernetes events

## 强制安全边界

- 不执行真实生产 Kubernetes 写操作。
- 不执行真实云资源写操作。
- 不删除数据。
- 不 truncate table。
- 不 flush 真实缓存。
- 不修改真实数据库。
- 所有执行动作使用 mock executor。
- L2 和 L3 动作必须人工审批。
- L3 审批必须二次确认。
- L4 动作直接拒绝，不能进入审批，也不能执行。

## 风险等级

| 等级 | 例子 | 策略 |
| --- | --- | --- |
| L0 | 查询 metrics/logs/traces/git | 自动允许 |
| L1 | 生成报告、创建 ticket、cache warmup | 自动允许 |
| L2 | restart pod、scale deployment、restart service | 需要人工审批 |
| L3 | rollback release、enable rate limit | 审批 + 二次确认 |
| L4 | delete data、truncate table、flush cache、modify database | 直接拒绝 |

## L3 二次确认

L3 approval 请求必须满足：

```json
{
  "risk_ack": true,
  "confirm_action_type": "<action.type>",
  "confirm_target": "<action.target>"
}
```

字段缺失或不匹配应返回 400。重复审批应返回 409。

## FakeLLM 与真实 LLM

CI、单元测试和 smoke eval 必须使用 FakeLLM。真实 LLM 只允许用于手动 demo 或 manual full eval，不作为稳定 CI gate。

配置项中存在真实 provider 入口，但默认 `LLM_PROVIDER=fake`。如果手动启用真实 LLM，应确保：

- prompt 仍然引用 evidence ID 和 Runbook chunk ID；
- 不把大量原始日志直接塞入 prompt；
- guardrail 决策仍由确定性规则执行；
- 真实 LLM 输出不能直接授权动作执行。

## 只读真实后端的边界

当前实现中存在可选真实 read backend，例如 K8s live diagnostics、DB diagnostics、Trace/Deployment backend。这些后端只能用于读取或诊断，不应实现生产写操作。

DB diagnostics 若启用 live，应使用只读账号，并保持 SQL 只读校验、statement timeout 和 transaction read only。K8s live diagnostics 不应做 patch/delete/scale/rollout 等写操作。

## Roadmap 内容的处理

`plans/11-roadmap/` 描述 Phase 1-8 post-MVP 扩展。除非用户明确要求实现某个具体 slice，否则不能把 roadmap 中的能力自动当作当前行为，尤其不能默认放宽：

- 真实 LLM 作为 CI gate；
- 真实 K8s/cloud 写操作；
- RBAC/SSO；
- 模型 fine-tuning；
- 自动执行高风险生产变更。
