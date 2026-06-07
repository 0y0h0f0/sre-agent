# 状态枚举与 ID 前缀

## ID 前缀

| 前缀 | 资源 |
| --- | --- |
| `req_` | request id |
| `inc_` | incident |
| `run_` | agent run |
| `nd_` | agent run node |
| `tool_` | tool call |
| `evi_` / `evd_` | evidence |
| `act_` | action |
| `apv_` | approval |
| `rpt_` | incident report |
| `chk_` | runbook chunk / checkpoint-like chunk ID |
| `mem_` | memory item |
| `eval_` | eval run |
| `draft_` | runbook draft |
| `ver_` | runbook version |
| `audit_` | audit log |
| `comment_` | incident comment |
| `ann_` | evidence annotation |
| `apig_` | approval group |
| `apik_` | API key |

实际前缀以 `packages/common/ids.py` 和各 repository/service 调用为准；新增资源应使用可读前缀，不使用数据库自增 ID 暴露给 API。

## Severity

```text
P1
P2
P3
P4
```

## IncidentStatus

```text
open
diagnosing
waiting_approval
mitigated
resolved
failed
```

## AgentRunStatus

```text
queued
running
waiting_approval
succeeded
failed
cancelled
```

## RiskLevel

```text
L0
L1
L2
L3
L4
```

## ActionStatus

```text
proposed
blocked
waiting_approval
approved
rejected
executing
succeeded
failed
```

## ApprovalStatus

```text
waiting
approved
rejected
expired
```

## ToolStatus

```text
succeeded
failed
degraded
timeout
```

## 状态流转概要

Incident：

```text
open -> diagnosing -> waiting_approval -> mitigated/resolved
open -> diagnosing -> failed
```

Agent run：

```text
queued -> running -> waiting_approval -> running -> succeeded
queued -> running -> succeeded
queued -> running -> failed
```

Action：

```text
proposed -> waiting_approval -> approved -> succeeded
proposed -> waiting_approval -> rejected
proposed -> blocked
```

Approval：

```text
waiting -> approved
waiting -> rejected
waiting -> expired
```
