# 后端架构

## 分层原则

后端采用 FastAPI + service + repository 的分层结构。

| 层 | 路径 | 责任 |
| --- | --- | --- |
| Router | `apps/api/routers` | 路由声明、依赖注入、调用 service |
| Schema | `apps/api/schemas` | Pydantic 请求/响应模型、枚举、分页和错误模型 |
| Service | `apps/api/services` | 业务规则、事务边界、跨 repository 协作、任务入队 |
| Repository | `packages/db/repositories` | SQLAlchemy 查询和写入 |
| Model | `packages/db/models.py` | 数据库表结构 |
| Worker | `apps/worker` | 异步诊断、审批恢复、通知、评测 |

Router 应保持薄层，不直接写复杂业务逻辑。数据库读写应尽量落在 repository，业务决策落在 service。

## FastAPI 应用入口

`apps/api/main.py` 创建应用并注册：

- CORS middleware。
- GZip middleware。
- request id middleware。
- API key middleware。
- `AppError` 统一错误处理。
- `RequestValidationError` 统一校验错误处理。
- health、alerts、incidents、agent runs、runbooks、reports、approvals、actions、comments、approval groups、api keys、evals 和 WebSocket router。

## Request ID

所有请求都会获得 `X-Request-Id`：

- 如果客户端传入该 header，服务端沿用。
- 如果缺失，服务端生成 `req_` 前缀 ID。
- 响应 header 总是返回最终 request id。
- 错误响应 body 中也包含同一个 request id。

## 事务和 session

API 通过依赖注入获取数据库 session。Worker 任务通过 `SessionLocal()` 创建独立 session。Agent 节点不得自行创建 session，必须通过 `AgentDeps.db` 使用调用方注入的 session。

## 写接口规则

- 写接口应接受并返回 `X-Request-Id`。
- `POST /api/alerts` 只创建 incident/agent run 并入队，不在线内运行 LangGraph。
- report regenerate 必须创建新版本，不覆盖旧报告。
- approval approve/reject 必须验证状态，重复决策返回 conflict。
- action execute 必须重新检查 action risk/status/approval，不能只依赖前端状态。

## 服务对象

主要 service：

| Service | 责任 |
| --- | --- |
| `AlertService` | 告警归一化、fingerprint 去重、创建 incident/run、入队 |
| `IncidentService` | 事故列表/详情、手动诊断、run 列表 |
| `AgentRunService` | agent run 详情、节点轨迹、工具调用摘要 |
| `ApprovalService` | 审批列表、L3 二次确认、审批恢复入队、邮件 token |
| `ActionService` | 动作详情、mock executor 执行校验 |
| `RunbookService` | Runbook 入库、检索、草稿、版本 |
| `ReportService` | 获取最新报告、生成新版本报告 |
| `EmailNotificationService` | 邮件事件排队、模板渲染、发送和重试状态 |
| `FeedbackService` | NFA、根因修正、动作反馈、跨事故关联 |
| `CommentService` | incident 评论、证据标注 |
| `ApprovalGroupService` | 审批组管理 |
| `ApiKeyService` | API key 创建、校验、撤销、last_used 更新 |
| `EvalService` | eval run 和 shadow run |

## 依赖注入

`apps/api/dependencies.py` 提供数据库、settings 和任务入队函数依赖。测试可以替换入队函数，使 API 测试不需要真实 Celery worker。

## 数据一致性重点

- open incident 按 fingerprint 唯一去重。
- agent run 终态不可被重复任务重新执行。
- waiting approval run 只能通过 resume task 恢复。
- L4 action 状态应保持 blocked/rejected，不生成 approval。
- report 按 `(incident_id, version)` 唯一。
- raw API key 只在创建响应中返回一次，数据库只保存 hash。
