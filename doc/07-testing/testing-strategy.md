# 测试策略

## 覆盖率门禁

后端：

```bash
pytest tests/unit tests/integration --cov=apps --cov=packages --cov-report=term-missing --cov-report=xml --cov-fail-under=80
```

前端：

```bash
npm run test:coverage
```

端到端：

```bash
npm run test:e2e
```

## 单元测试

必须覆盖：

- Pydantic schema。
- ID 生成和时间工具。
- repository 查询条件。
- PromQL 和 LogQL 构造。
- 工具超时和降级。
- Runbook splitter、metadata、rerank。
- Memory cache key、context hash、compression trigger。
- LangGraph 每个节点。
- Guardrail 风险矩阵。
- Celery 幂等逻辑。

## 集成测试

必须覆盖：

- API 创建 incident 并入队。
- 重复 fingerprint 去重。
- Celery eager 执行诊断。
- mock Prometheus 和 mock Loki 查询。
- pgvector Runbook 检索。
- 审批通过后恢复。
- 审批拒绝后生成替代方案。
- 工具失败后 run 记录 degraded result。

## 契约测试

- 生成 OpenAPI schema。
- 与 committed snapshot 比较。
- 前端 API 类型从 schema 生成或手写后由测试校验。
- 错误响应格式必须一致。

## E2E 测试

使用 Playwright，默认 FakeLLM。

四条链路：

1. DB 连接池耗尽。
2. 发布后 5xx。
3. Redis 缓存雪崩。
4. Pod 异常重启。

断言：

- incident 创建成功。
- Agent run 节点可见。
- evidence 可见。
- action 风险等级正确。
- 审批行为正确。
- report 生成成功。

## Token 与记忆测试

必须单独测试：

- 静态 prompt hash 稳定。
- schema hash 稳定。
- evidence 排序后 hash 稳定。
- 相同 Runbook chunk 命中 cache。
- 日志超过预算触发压缩。
- 压缩结果保留 evidence id。
- 不相关 service memory 被过滤。
- 历史记忆不能单独支撑 root cause。

## 推荐测试目录

```text
tests/
  unit/
    test_schemas.py
    test_guardrails.py
    test_context_budget.py
    test_memory_cache.py
    test_tools_metrics.py
    test_tools_logs.py
    test_rag_splitter.py
    test_agent_nodes.py
  integration/
    test_alert_api.py
    test_celery_diagnosis.py
    test_runbook_search.py
    test_approval_resume.py
  contract/
    test_openapi_snapshot.py
  e2e/
    test_incident_flows.spec.ts
```

## CI 顺序

1. Ruff。
2. mypy。
3. 后端单元测试。
4. 后端集成测试。
5. 前端单元测试和覆盖率。
6. OpenAPI 契约测试。
7. Playwright smoke。
8. eval smoke。
