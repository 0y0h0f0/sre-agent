# 后端项目结构

## 目标结构

```text
apps/
  api/
    main.py
    dependencies.py
    routers/
      alerts.py
      incidents.py
      agent_runs.py
      approvals.py
      actions.py
      runbooks.py
      reports.py
      health.py
    schemas/
      common.py
      alerts.py
      incidents.py
      agent_runs.py
      approvals.py
      actions.py
      runbooks.py
      reports.py
    services/
      alert_service.py
      incident_service.py
      approval_service.py
      action_service.py
      runbook_service.py
      report_service.py
  worker/
    main.py
    celery_app.py
    tasks.py
packages/
  db/
    base.py
    session.py
    models.py
    repositories/
      incidents.py
      agent_runs.py
      tool_calls.py
      actions.py
      approvals.py
      runbooks.py
  common/
    ids.py
    time.py
    errors.py
    settings.py
```

## 分层规则

- Router 只做 HTTP 参数接收、响应转换和错误映射。
- Service 负责业务事务、幂等、任务入队、调用 repository。
- Repository 只封装数据库读写，不包含业务策略。
- Pydantic schema 与 SQLAlchemy model 分离。
- Celery task 调用 service 或 Agent runner，不直接写复杂 SQL。
- 所有外部依赖通过 dependency 注入，测试中可以替换为 fake。

## 统一 ID

实现 `packages/common/ids.py`：

```python
def new_id(prefix: str) -> str:
    ...
```

固定前缀：

- `inc_`：incident。
- `run_`：agent run。
- `tool_`：tool call。
- `evi_`：evidence item。
- `act_`：action。
- `apv_`：approval。
- `rpt_`：report。
- `chk_`：runbook chunk。
- `mem_`：memory event。

## 配置

实现 `packages/common/settings.py`，使用 Pydantic settings：

```python
class Settings(BaseSettings):
    database_url: str
    redis_url: str
    celery_broker_url: str
    celery_result_backend: str
    prometheus_url: str
    loki_url: str
    embedding_provider: str = "fake"
    llm_provider: str = "fake"
    llm_model: str = "fake-diagnosis-model"
    token_budget_total: int = 32000
    token_budget_prompt: int = 12000
    token_cache_enabled: bool = True
```

## 错误处理

实现统一异常：

- `AppError(code, message, status_code, details)`。
- `ValidationAppError`。
- `NotFoundError`。
- `ConflictError`。
- `DependencyUnavailableError`。
- `ApprovalRequiredError`。

HTTP 错误响应固定：

```json
{
  "error": {
    "code": "VALIDATION_ERROR",
    "message": "service is required",
    "request_id": "req_123",
    "details": {}
  }
}
```

## 事务约定

- 创建 incident 和 agent_run 必须在同一事务中完成。
- 入队 Celery 前必须落库；入队失败时 incident 状态置为 `failed` 或记录 retry。
- 审批通过和恢复 LangGraph 必须防重复提交。
- action 执行结果和 audit log 必须同事务写入。

## 代码生成顺序

1. `common/settings.py`、`common/ids.py`、`common/errors.py`。
2. `db/session.py`、`db/base.py`。
3. `db/models.py`。
4. Pydantic schemas。
5. repositories。
6. services。
7. routers。
8. Celery app 和 tasks。
9. tests。
